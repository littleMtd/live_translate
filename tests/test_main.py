"""Unit tests for main.py helpers.

These tests exercise pieces of `main.py` that the integration suite can't
reach without real audio hardware. The motivating regression is A3 (see
OPTIMIZATION_ACTION_PLAN.md §6) — the STT-only printer loop accessed
`item['text']` on a `SentenceEvent` dataclass that no longer implements
`__getitem__`, raising `TypeError` on every line in production.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from unittest.mock import MagicMock

# Stub heavy optional modules so `import main` succeeds without a full venv.
for _mod in ("sounddevice", "soundfile", "funasr", "groq", "anthropic"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from modules.pipeline_events import SentenceEvent


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
