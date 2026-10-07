"""Characterization of translator.start() ordering, drain and shutdown paths.

Captured before the Phase 2b split of the start() closure so the extracted
coordinator/worker classes must preserve these lifecycle semantics.
"""

from __future__ import annotations

import queue
import threading
import time
from unittest.mock import patch

import modules.translator as translator_module
from modules.translator import TranslationOutcome


def _outcome(text: str, target: str | None) -> TranslationOutcome:
    return TranslationOutcome(
        source_text=text,
        target_text=target,
        status="success" if target else "failed",
        result_source="api",
        cache_status="miss",
        incomplete=False,
        engine="fake",
        model="fake-model",
    )


_TARGETS = {"하나": "譯一", "둘": "譯二", "셋": "譯三"}


def _fake_translator(delays: dict[str, float] | None = None, block: threading.Event | None = None):
    class _FakeTranslator:
        def __init__(self, shared_state=None):
            pass

        def translate_event(self, text, incomplete=False, **_kwargs):
            if block is not None:
                block.wait(5)
            time.sleep((delays or {}).get(text, 0.0))
            return _outcome(text, _TARGETS[text])

    return _FakeTranslator


def _drain_subtitles(subtitle_q: queue.Queue) -> list:
    items = []
    while True:
        try:
            items.append(subtitle_q.get_nowait())
        except queue.Empty:
            return items


def _translation_events(events) -> list[dict]:
    return [
        call.kwargs for call in events.emit.call_args_list
        if call.args and call.args[0] == "translation"
    ]


def test_final_worker_metadata_survives_ordered_publication():
    sentence_q, subtitle_q = queue.Queue(), queue.Queue()
    stop, upstream_done = threading.Event(), threading.Event()
    with patch.object(translator_module, "Translator", _fake_translator()), \
            patch.object(translator_module, "runtime_events") as events:
        thread = translator_module.start(
            sentence_q, subtitle_q, stop, upstream_done_event=upstream_done
        )
        sentence_q.put("하나")
        stop.set()
        upstream_done.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert _drain_subtitles(subtitle_q) == ["譯一"]
    emitted = _translation_events(events)
    assert len(emitted) == 1
    event = emitted[0]
    assert event["sequence_id"] == 0
    assert event["status"] == "success"
    assert event["worker_started_at_utc"]
    assert event["worker_completed_at_utc"]
    assert event["translation_submitted_at_utc"]
    assert event["translation_worker_id"]
    assert event["activity_snapshot_stage"] == "worker_fallback"
    assert event["activity_snapshot_fallback_used"] is True


def test_stop_drains_accepted_tail_in_sequence_order():
    sentence_q, subtitle_q = queue.Queue(), queue.Queue()
    stop, upstream_done = threading.Event(), threading.Event()
    # The first sentence finishes last; output must still follow input order.
    fake = _fake_translator({"하나": 0.3, "둘": 0.0, "셋": 0.1})
    with patch.object(translator_module, "Translator", fake), \
            patch.object(translator_module, "runtime_events"):
        thread = translator_module.start(
            sentence_q, subtitle_q, stop, upstream_done_event=upstream_done
        )
        for text in ("하나", "둘", "셋"):
            sentence_q.put(text)
        stop.set()
        upstream_done.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert _drain_subtitles(subtitle_q) == ["譯一", "譯二", "譯三"]


def test_pause_at_stop_discards_queued_sentences():
    sentence_q, subtitle_q = queue.Queue(), queue.Queue()
    stop, pause = threading.Event(), threading.Event()
    pause.set()
    fake = _fake_translator()
    with patch.object(translator_module, "Translator", fake), \
            patch.object(translator_module, "runtime_events") as events:
        thread = translator_module.start(sentence_q, subtitle_q, stop, pause)
        for text in ("하나", "둘"):
            sentence_q.put(text)
        time.sleep(0.2)
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert sentence_q.empty()
    assert _drain_subtitles(subtitle_q) == []
    assert _translation_events(events) == []


def test_failed_worker_future_emits_ordered_failure_before_later_success():
    sentence_q, subtitle_q = queue.Queue(), queue.Queue()
    stop, upstream_done = threading.Event(), threading.Event()
    calls = {"n": 0}
    real_reset = translator_module.reset_corrections

    def reset_once_raises():
        # Outside translate_item's try: the future itself raises.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker crashed before try")
        return real_reset()

    fake = _fake_translator()
    with patch.object(translator_module, "Translator", fake), \
            patch.object(translator_module, "reset_corrections", reset_once_raises), \
            patch.object(translator_module, "runtime_events") as events, \
            patch.object(translator_module.metrics, "increment") as increment:
        thread = translator_module.start(
            sentence_q, subtitle_q, stop, upstream_done_event=upstream_done
        )
        sentence_q.put("하나")
        time.sleep(0.2)
        sentence_q.put("둘")
        time.sleep(0.2)
        stop.set()
        upstream_done.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert ("translation.future_failed",) in [c.args for c in increment.call_args_list]
    emitted = _translation_events(events)
    assert [e["sequence_id"] for e in emitted] == [0, 1]
    assert emitted[0]["status"] == "failed"
    assert emitted[0]["subtitle_emitted"] is False
    assert emitted[1]["subtitle_emitted"] is True
    assert _drain_subtitles(subtitle_q) == ["譯二"]


def test_drain_timeout_cancels_pending_and_logs_warning():
    sentence_q, subtitle_q = queue.Queue(), queue.Queue()
    stop, upstream_done = threading.Event(), threading.Event()
    release = threading.Event()
    fake = _fake_translator(block=release)
    warnings = []
    with patch.object(translator_module, "Translator", fake), \
            patch.object(translator_module, "runtime_events"), \
            patch.object(translator_module, "_stop_drain_timeout_sec", return_value=0.3), \
            patch.object(
                translator_module.log, "warning",
                side_effect=lambda message, *args: warnings.append(message % args),
            ):
        thread = translator_module.start(
            sentence_q, subtitle_q, stop, upstream_done_event=upstream_done
        )
        for text in ("하나", "둘", "셋"):
            sentence_q.put(text)
        time.sleep(0.2)
        started = time.monotonic()
        stop.set()
        upstream_done.set()
        thread.join(timeout=5)
        elapsed = time.monotonic() - started
        release.set()

    assert not thread.is_alive()
    assert elapsed < 2
    assert any(w.startswith("Stop drain timed out") for w in warnings)
    assert _drain_subtitles(subtitle_q) == []
