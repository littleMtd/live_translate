"""Unit tests for main.py helpers.

These tests exercise pieces of `main.py` that the integration suite can't
reach without real audio hardware. The motivating regression is A3 (see
OPTIMIZATION_ACTION_PLAN.md §6) — the STT-only printer loop accessed
`item['text']` on a `SentenceEvent` dataclass that no longer implements
`__getitem__`, raising `TypeError` on every line in production.
"""
from __future__ import annotations

from dataclasses import replace
import queue
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

# Stub heavy optional modules so `import main` succeeds without a full venv.
for _mod in ("sounddevice", "soundfile", "funasr", "groq", "anthropic"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from modules.pipeline_events import SentenceEvent


@pytest.fixture
def isolate_scene_vision_startup_validation(monkeypatch):
    import main as main_module

    monkeypatch.setattr(
        main_module,
        "_validate_scene_vision_config",
        lambda: None,
    )


def test_validate_config_accepts_nvidia_backend_without_engine_chain(
    monkeypatch,
    isolate_scene_vision_startup_validation,
):
    import main as main_module

    monkeypatch.setattr(main_module, "_selected_translation_backend", lambda: "nvidia")
    monkeypatch.setattr(main_module, "engine_is_configured", lambda name: name == "nvidia")

    main_module._validate_config(stt_only=False)


def test_validate_config_warns_for_missing_nvidia_fallback_key(
    monkeypatch,
    isolate_scene_vision_startup_validation,
):
    import main as main_module

    warnings = []
    original_chain = main_module.cfg.translation.engine_chain
    monkeypatch.setattr(main_module, "_selected_translation_backend", lambda: "nvidia")
    monkeypatch.setattr(main_module, "engine_is_configured", lambda name: name == "nvidia")
    monkeypatch.setattr(
        main_module.log,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    try:
        object.__setattr__(main_module.cfg.translation, "engine_chain", ("groq",))

        main_module._validate_config(stt_only=False)
    finally:
        object.__setattr__(main_module.cfg.translation, "engine_chain", original_chain)

    assert warnings == ["Engine 'groq' skipped - API key not set"]


def test_validate_config_rejects_nvidia_backend_without_key(
    monkeypatch,
    isolate_scene_vision_startup_validation,
):
    import main as main_module

    monkeypatch.setattr(main_module, "_selected_translation_backend", lambda: "nvidia")
    monkeypatch.setattr(main_module, "engine_is_configured", lambda _name: False)

    try:
        main_module._validate_config(stt_only=False)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("_validate_config should exit when NVIDIA_API_KEY is missing")


def test_validate_config_warns_when_deepl_key_is_missing(
    monkeypatch,
    isolate_scene_vision_startup_validation,
):
    import main as main_module

    warnings = []
    original_chain = main_module.cfg.translation.engine_chain
    monkeypatch.setattr(main_module, "_selected_translation_backend", lambda: "nvidia")
    monkeypatch.setattr(main_module, "engine_is_configured", lambda name: name == "nvidia")
    monkeypatch.setattr(
        main_module.log,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    try:
        object.__setattr__(main_module.cfg.translation, "engine_chain", ("deepl",))
        main_module._validate_config(stt_only=False)
    finally:
        object.__setattr__(main_module.cfg.translation, "engine_chain", original_chain)

    assert warnings == ["Engine 'deepl' skipped - API key not set"]


def test_scene_vision_startup_validation_rejects_missing_route_key(monkeypatch):
    import main as main_module
    import modules.scene_vision as scene_vision

    monkeypatch.setattr(
        scene_vision,
        "missing_vision_route_credentials",
        lambda: ("openrouter:vision-model",),
    )

    with pytest.raises(SystemExit) as captured:
        main_module._validate_scene_vision_config()

    assert captured.value.code == 1


def test_scene_vision_startup_validation_accepts_configured_routes(monkeypatch):
    import main as main_module
    import modules.scene_vision as scene_vision

    monkeypatch.setattr(
        scene_vision,
        "missing_vision_route_credentials",
        lambda: (),
    )

    main_module._validate_scene_vision_config()


def test_apply_listen_mode_config_relaxes_stt_filters():
    import main as main_module

    original_stt = main_module.cfg.stt
    test_stt = replace(
        original_stt,
        groq_prompt="speech prompt",
        no_speech_threshold=0.6,
        avg_logprob_threshold=-1.0,
        listen_groq_prompt="lyrics prompt",
        listen_no_speech_threshold=0.8,
        listen_avg_logprob_threshold=-2.0,
    )
    object.__setattr__(main_module.cfg, "stt", test_stt)

    try:
        main_module._apply_listen_mode_config()

        assert main_module.cfg.stt.groq_prompt == "lyrics prompt"
        assert main_module.cfg.stt.no_speech_threshold == 0.8
        assert main_module.cfg.stt.avg_logprob_threshold == -2.0
    finally:
        object.__setattr__(main_module.cfg, "stt", original_stt)


def test_donation_ocr_command_respects_profile_enablement():
    import main as main_module

    original = main_module.cfg.translation.use_profile
    app_path = main_module.Path("donation_ocr/app.py")
    try:
        object.__setattr__(main_module.cfg.translation, "use_profile", False)
        assert main_module._donation_ocr_command(app_path)[-1] == ""

        object.__setattr__(main_module.cfg.translation, "use_profile", True)
        assert main_module._donation_ocr_command(app_path)[-1] == main_module.cfg.active_streamer_profile
    finally:
        object.__setattr__(main_module.cfg.translation, "use_profile", original)


def _wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    """Poll `predicate` until it returns truthy or the deadline elapses.

    Returns True if the predicate became truthy in time, False otherwise.
    Avoids fixed long sleeps so the test stays fast on green builds and
    has a bounded ceiling on failures.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_stt_printer_handles_sentence_event(tmp_path, monkeypatch):
    """A3 regression: `_stt_printer` must consume `SentenceEvent` instances
    using attribute access (`item.text`, `item.incomplete`).

    Before the fix this loop did `item['text']`, which raised
    `TypeError: 'SentenceEvent' object is not subscriptable` and killed the
    STT-only mode on its very first emission.
    """
    import main as main_module

    # Redirect log output into the pytest tmp dir so the test never touches
    # the real logs/ directory.
    monkeypatch.setattr(main_module, "_LOG_DIR", tmp_path)

    sentence_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    sentence_queue.put(SentenceEvent(text="안녕하세요",     incomplete=False))
    sentence_queue.put(SentenceEvent(text="지금 게임 하고", incomplete=True))

    t = main_module._stt_printer(sentence_queue, stop_event)

    try:
        # Wait for both lines to land in the on-disk log. We poll instead of
        # sleeping a fixed duration so the test passes as soon as the printer
        # has done its job.
        def _both_lines_written() -> bool:
            files = list(tmp_path.glob("stt_*.txt"))
            if not files:
                return False
            content = files[0].read_text(encoding="utf-8")
            return "안녕하세요" in content and "지금 게임 하고" in content

        assert _wait_until(_both_lines_written, timeout=5.0), (
            "STT printer did not write both sentences to disk in time"
        )
    finally:
        stop_event.set()
        # The printer's queue.get uses a 1 s timeout, so give the thread a
        # bit more than that to notice stop_event and exit cleanly.
        t.join(timeout=3.0)

    assert not t.is_alive(), "STT printer thread is still alive after stop_event"

    log_files = list(tmp_path.glob("stt_*.txt"))
    assert len(log_files) == 1, f"expected one log file, got {[p.name for p in log_files]}"

    content = log_files[0].read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) == 2, f"expected 2 lines, got {lines!r}"

    # The complete sentence must appear WITHOUT the `[incomplete]` marker.
    complete_lines   = [line for line in lines if "안녕하세요"     in line]
    incomplete_lines = [line for line in lines if "지금 게임 하고" in line]
    assert len(complete_lines)   == 1
    assert len(incomplete_lines) == 1
    assert "[incomplete]" not in complete_lines[0]
    assert "[incomplete]"     in incomplete_lines[0]


def test_stt_printer_exits_on_stop_event_when_queue_idle(tmp_path, monkeypatch):
    """The printer must not block forever on an empty queue once stop_event
    is set — its internal `queue.get(timeout=1)` should round-trip and the
    while-loop should exit on the next iteration."""
    import main as main_module

    monkeypatch.setattr(main_module, "_LOG_DIR", tmp_path)

    sentence_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    t = main_module._stt_printer(sentence_queue, stop_event)

    # Give the thread a beat to enter its loop, then signal stop.
    assert _wait_until(lambda: t.is_alive(), timeout=2.0), "printer thread never started"
    stop_event.set()
    t.join(timeout=3.0)

    assert not t.is_alive(), "STT printer did not exit after stop_event with empty queue"
