"""Previews never jump ahead of earlier finals, and a preview whose final is not shown is withdrawn.

Whole-project review 2026-10-26: previews bypassed the ordered emitter (about 1 % of finals read
N+1, N, N+1 on screen), and a suppressed or failed final left its unchecked preview up for 30 s.
"""
import queue
import threading
from dataclasses import replace
from unittest.mock import patch

import modules.translator as translator
from modules.provisional_subtitles import ProvisionalStore, SubtitlePayload
from modules.subtitle_display import SubtitleWindow


def _worker(store=None):
    return translator._ProvisionalWorker(None, store or ProvisionalStore(), threading.Event(), queue.Queue())


def test_preview_waits_for_earlier_finals_then_shows():
    worker = _worker()
    state = {"emitted": False}
    worker._earlier_finals_shown = lambda: state["emitted"]
    timer = threading.Timer(0.1, lambda: state.update(emitted=True))
    timer.start()
    assert worker._wait_for_earlier_finals("provisional:utt-2") is True
    timer.join()


def test_preview_is_dropped_when_its_own_final_arrives_first_or_on_timeout(monkeypatch):
    store = ProvisionalStore()
    worker = _worker(store)
    worker._earlier_finals_shown = lambda: False
    store.close("provisional:utt-2")                      # its own final already closed it
    assert worker._wait_for_earlier_finals("provisional:utt-2") is False
    monkeypatch.setattr(translator, "_PREVIEW_ORDER_WAIT_SEC", 0.05)
    assert worker._wait_for_earlier_finals("provisional:utt-3") is False


def test_wait_ends_on_shutdown():
    worker = _worker()
    worker._earlier_finals_shown = lambda: False
    worker._stop_event.set()
    assert worker._wait_for_earlier_finals("provisional:utt-4") is False


def test_duplicate_final_also_withdraws_its_preview():
    subtitles = queue.Queue()
    emitter = translator._OrderedEmitter(translator._new_translator_shared_state(), subtitles)
    with patch.object(translator.runtime_events, "emit"):
        emitter.emit_completed(_completed("同一句", provisional_id="provisional:utt-1"))
        emitter.emit_completed(_completed("同一句", provisional_id="provisional:utt-2"))
    assert [subtitles.get_nowait() for _ in range(2)] == [
        SubtitlePayload("同一句", "provisional:utt-1", revision=1, phase="final"),
        SubtitlePayload("", "provisional:utt-2", revision=1, phase="final"),
    ]


def test_no_gate_means_no_wait():
    worker = _worker()
    worker._earlier_finals_shown = None
    assert worker._wait_for_earlier_finals("provisional:utt-1") is True


def test_abandoned_preview_is_never_enqueued(monkeypatch):
    store = ProvisionalStore()
    worker = _worker(store)
    worker._earlier_finals_shown = lambda: False
    monkeypatch.setattr(translator, "_PREVIEW_ORDER_WAIT_SEC", 0.0)
    request = type("Req", (), {"provisional_id": "provisional:utt-9"})()
    events = []
    with patch.object(translator.runtime_events, "emit", lambda *a, **k: events.append((a, k))):
        worker._publish_candidate(request, None, SubtitlePayload("預覽", "provisional:utt-9"), None, None,
                                  None, "預覽", 0.0, 0.0, {}, {}, "")
    assert worker._subtitle_queue.empty()
    assert store.is_closed("provisional:utt-9")
    assert events[-1][1]["action"] == "cancelled_late" and events[-1][1]["reason"] == "earlier_final_pending"


def _completed(target, provisional_id="provisional:utt-5", status="success"):
    item = translator._failed_completion(0)
    outcome = replace(item.outcome, source_text="원문", target_text=target, status=status)
    return replace(item, outcome=outcome, metadata={**item.metadata, "provisional_id": provisional_id})


def test_failed_final_withdraws_its_preview():
    subtitles = queue.Queue()
    emitter = translator._OrderedEmitter(translator._new_translator_shared_state(), subtitles)
    with patch.object(translator.runtime_events, "emit"):
        emitter.emit_completed(_completed(None, status="failed"))
    payload = subtitles.get_nowait()
    assert payload == SubtitlePayload(text="", subtitle_id="provisional:utt-5", revision=1, phase="final")


def test_shown_final_does_not_withdraw():
    subtitles = queue.Queue()
    emitter = translator._OrderedEmitter(translator._new_translator_shared_state(), subtitles)
    with patch.object(translator.runtime_events, "emit"):
        emitter.emit_completed(_completed("正式翻譯"))
    assert subtitles.get_nowait().text == "正式翻譯"
    assert subtitles.empty()


class _Root:
    def after(self, ms, callback):
        return "job"

    def after_cancel(self, job):
        pass


def _window():
    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    window._root = _Root()
    drawn = []
    window._draw_lines = lambda lines, justify: drawn.append([t for t, _f, _c in lines if t.strip()])
    window._refresh_overlay = lambda now: None
    return window, drawn


def test_display_withdraws_shown_preview_and_keeps_the_last_real_line():
    window, drawn = _window()
    window._queue.put(SubtitlePayload("上一句正式", "provisional:utt-4", revision=1, phase="final"))
    window._poll()
    window._show_time = 0.0                               # skip the reading-speed hold
    window._queue.put(SubtitlePayload("沒檢查過的預覽", "provisional:utt-5", revision=0, phase="provisional"))
    window._poll()
    assert drawn[-1] == ["上一句正式", "沒檢查過的預覽"]
    with patch("modules.subtitle_display.runtime_events.emit"):
        window._queue.put(SubtitlePayload("", "provisional:utt-5", revision=1, phase="final"))
        window._poll()
    assert drawn[-1] == ["上一句正式"]


def test_withdrawing_the_current_line_keeps_a_different_pending_one():
    window, _drawn = _window()
    window._queue.put(SubtitlePayload("預覽", "provisional:utt-5", revision=0, phase="provisional"))
    window._poll()
    window._pending_text = SubtitlePayload("下一句", "provisional:utt-6", revision=1, phase="final")
    with patch("modules.subtitle_display.runtime_events.emit"):
        window._withdraw("provisional:utt-5")
    assert window._pending_text.subtitle_id == "provisional:utt-6"
    assert window._show_min_ms == 0


def test_display_drops_a_pending_preview_that_is_withdrawn():
    window, _drawn = _window()
    window._pending_text = SubtitlePayload("等待中的預覽", "provisional:utt-7", revision=0, phase="provisional")
    with patch("modules.subtitle_display.runtime_events.emit"):
        window._withdraw("provisional:utt-7")
    assert window._pending_text is None
