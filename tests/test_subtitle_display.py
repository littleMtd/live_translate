import queue
import threading
import time

from modules.subtitle_display import SubtitleWindow
from modules.provisional_subtitles import SubtitlePayload


class _FakeRoot:
    def __init__(self):
        self.after_calls = []
        self.cancelled = []

    def after(self, ms, callback):
        job = f"job-{len(self.after_calls) + 1}"
        self.after_calls.append((job, ms, callback))
        return job

    def after_cancel(self, job):
        self.cancelled.append(job)


def _window_with_fake_root():
    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    root = _FakeRoot()
    window._root = root
    drawn = []
    window._draw_outlined_text = drawn.append
    return window, root, drawn


def test_pause_flash_stays_visible_until_resume():
    window, root, drawn = _window_with_fake_root()

    window._flash("old")
    assert window._hide_job == "job-1"

    window._flash("paused", ms=None)

    assert drawn == ["old", "paused"]
    assert root.cancelled == ["job-1"]
    assert window._hide_job is None
    assert len(root.after_calls) == 1


def test_toggle_pause_uses_persistent_pause_indicator_and_resume_flashes():
    window, root, drawn = _window_with_fake_root()

    window._toggle_translation()

    assert window._translating is False
    assert window._pause.is_set()
    assert drawn[-1] == "\u23f8"
    assert window._hide_job is None
    assert root.after_calls == []

    window._toggle_translation()

    assert window._translating is True
    assert not window._pause.is_set()
    assert drawn[-1] == "\u25b6"
    assert window._hide_job == "job-1"
    assert root.after_calls[0][1] == 1500


def test_toggle_discards_subtitle_held_for_reading_speed_guard():
    window, _root, _drawn = _window_with_fake_root()
    window._pending_text = "stale subtitle"

    window._toggle_translation()

    assert window._pending_text is None


def test_final_revision_replaces_visible_provisional_without_duplicate_hold():
    window, _root, _drawn = _window_with_fake_root()
    shown = []
    window._show = shown.append
    subtitle_id = "provisional:utt-1"

    window._queue.put(
        SubtitlePayload("暫定字幕", subtitle_id, revision=0, phase="provisional")
    )
    window._poll()
    window._queue.put(
        SubtitlePayload("最終字幕", subtitle_id, revision=1, phase="final")
    )
    window._poll()

    assert shown == ["暫定字幕", "最終字幕"]
    assert window._current_subtitle_id == subtitle_id
    assert window._pending_text is None


def test_late_provisional_cannot_replace_displayed_final_revision():
    window, _root, _drawn = _window_with_fake_root()
    shown = []
    window._show = shown.append
    subtitle_id = "provisional:utt-late"

    window._queue.put(SubtitlePayload("final", subtitle_id, revision=1, phase="final"))
    window._poll()
    window._queue.put(SubtitlePayload("late preview", subtitle_id, revision=0, phase="provisional"))
    window._poll()

    assert shown == ["final"]
    assert window._current_subtitle_revision == 1


def test_late_provisional_cannot_replace_pending_final_revision():
    window, _root, _drawn = _window_with_fake_root()
    window._show_min_ms = 60_000
    window._show_time = time.monotonic()
    subtitle_id = "provisional:utt-pending"

    window._queue.put(SubtitlePayload("final", subtitle_id, revision=1, phase="final"))
    window._queue.put(SubtitlePayload("late preview", subtitle_id, revision=0, phase="provisional"))
    window._poll()

    assert window._pending_text == SubtitlePayload(
        "final", subtitle_id, revision=1, phase="final"
    )


def test_newer_revision_still_replaces_pending_older_revision():
    window, _root, _drawn = _window_with_fake_root()
    window._show_min_ms = 60_000
    window._show_time = time.monotonic()
    subtitle_id = "provisional:utt-forward"

    window._queue.put(SubtitlePayload("preview", subtitle_id, revision=0, phase="provisional"))
    window._queue.put(SubtitlePayload("final", subtitle_id, revision=1, phase="final"))
    window._poll()

    assert window._pending_text == SubtitlePayload(
        "final", subtitle_id, revision=1, phase="final"
    )


def _overlay_window(monkeypatch, labels=None, records=()):
    from types import SimpleNamespace
    from modules import subtitle_display
    from modules.live_status import SubtitleRecord

    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    window._root = _FakeRoot()
    drawn = []
    window._draw_lines = lambda lines, justify: drawn.append((justify, [text for text, _f, _c in lines]))
    box = {"labels": list(labels or [])}
    monkeypatch.setattr(subtitle_display.live_status, "flags", lambda profile_unresolved: list(box["labels"]))
    monkeypatch.setattr(subtitle_display.live_status, "recent_subtitles",
                        lambda limit: [SubtitleRecord(0.0, s, t) for s, t in records][-limit:])
    monkeypatch.setattr(subtitle_display.profile_state, "current",
                        lambda: SimpleNamespace(mode="auto", effective_profile_id="url"))
    return window, drawn, box


def test_status_line_appears_above_the_subtitle_and_disappears(monkeypatch):
    window, drawn, box = _overlay_window(monkeypatch)
    window._show("今天要玩什麼")
    assert drawn[-1] == ("center", ["今天要玩什麼"])
    box["labels"] = ["Groq 辨識"]
    window._refresh_overlay(10.0)
    assert drawn[-1] == ("center", ["● Groq 辨識", "今天要玩什麼"])
    box["labels"] = []
    window._refresh_overlay(11.5)
    assert drawn[-1] == ("center", ["今天要玩什麼"])


def test_status_line_stays_when_the_subtitle_hides(monkeypatch):
    window, drawn, box = _overlay_window(monkeypatch, labels=["general"])
    window._refresh_overlay(10.0)
    window._show("你好")
    window._hide()
    assert drawn[-1] == ("center", ["● general"])


def test_right_click_history_shows_source_and_translation_and_closes(monkeypatch):
    window, drawn, _box = _overlay_window(monkeypatch, records=[("안녕", "你好"), ("", "只有中文")])
    window._show("現在的字幕")
    window._toggle_history()
    assert drawn[-1] == ("left", ["안녕", "你好", "只有中文"])
    window._toggle_history()  # right-click again
    assert drawn[-1] == ("center", ["現在的字幕"])


def test_subtitles_wait_in_the_queue_while_history_is_open(monkeypatch):
    from modules import subtitle_display

    window, drawn, _box = _overlay_window(monkeypatch, records=[("안녕", "你好")])
    events = []
    monkeypatch.setattr(subtitle_display.runtime_events, "emit", lambda *a, **k: events.append((a, k)))
    window._toggle_history()
    window._queue.put(SubtitlePayload(text="新字幕", subtitle_id="provisional:utt-9", phase="provisional"))
    window._poll()
    assert window._queue.qsize() == 1          # not drained, not "displayed"
    assert drawn[-1][0] == "left" and events == []
    window._history_until = time.monotonic() - 1  # timer expired
    window._poll()
    assert drawn[-1] == ("center", ["新字幕"])  # drawn only now, so its minimum display starts now
    assert [k["action"] for _a, k in events] == ["displayed"]
    assert window._show_time >= time.monotonic() - 1


def test_pause_during_history_closes_it_and_clears_the_status_line(monkeypatch):
    window, drawn, box = _overlay_window(monkeypatch, labels=["general"], records=[("안녕", "你好")])
    window._refresh_overlay(10.0)
    window._show("字幕")
    window._toggle_history()
    window._toggle_translation()  # pause
    assert window._history_until == 0.0
    window._refresh_overlay(12.0)  # paused: labels turn off
    assert drawn[-1] == ("center", ["⏸"])
    window._toggle_translation()  # resume
    assert drawn[-1] == ("center", ["▶"])
    box["labels"] = []
    window._refresh_overlay(14.0)
    cleared = []
    window._clear_canvas = lambda: cleared.append(True)
    window._hide()  # the resume flash timer
    assert cleared == [True] and window._visible_text == ""


def test_history_cannot_open_while_paused(monkeypatch):
    window, drawn, _box = _overlay_window(monkeypatch, records=[("안녕", "你好")])
    window._toggle_translation()  # pause
    window._toggle_history()
    assert window._history_until == 0.0
    assert drawn[-1] == ("center", ["⏸"])


def test_pause_symbol_survives_a_status_change(monkeypatch):
    window, drawn, box = _overlay_window(monkeypatch, labels=["general"])
    window._refresh_overlay(10.0)
    window._show("暫停前的字幕")
    window._flash("⏸", ms=None)
    window._translating = False  # paused: the indicator turns off
    window._refresh_overlay(12.0)
    assert drawn[-1] == ("center", ["⏸"])


def test_middle_click_bookmark_writes_subtitles_before_and_after(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from modules import subtitle_display
    from modules.live_status import SubtitleRecord

    window, drawn, _box = _overlay_window(monkeypatch)
    root = window._root
    clock = {"now": 100.0}
    monkeypatch.setattr(subtitle_display.time, "monotonic", lambda: clock["now"])
    records = [SubtitleRecord(t, f"src{t:.0f}", f"zh{t:.0f}") for t in (90.0, 95.0, 99.0)]
    monkeypatch.setattr(subtitle_display.live_status, "recent_subtitles", lambda limit: list(records)[-limit:])
    monkeypatch.setattr(subtitle_display.profile_state, "current",
                        lambda: SimpleNamespace(mode="manual", effective_profile_id="isegye_lilpa"))

    window._mark()
    assert drawn[-1] == ("center", ["● ★ 已標記"])
    job, ms, write = root.after_calls[-1]
    assert ms == 20000 and not list(tmp_path.glob("bookmarks_*.md"))  # waits for what follows

    records.append(SubtitleRecord(105.0, "src105", "zh105"))
    del records[:3]  # the lines before the mark have scrolled out of the history
    write()
    text = next(tmp_path.glob("bookmarks_*.md")).read_text(encoding="utf-8")
    before, after = text.split("標記後：")
    assert "isegye_lilpa" in before and "src95" in before and "zh99" in before
    assert "src105" in after and "zh105" in after and "zh99" not in after
    assert window._pending_marks == []


def test_closing_writes_pending_bookmarks_immediately(monkeypatch, tmp_path):
    window, _drawn, _box = _overlay_window(monkeypatch)
    root = window._root
    root.destroy = lambda: None
    window._mark()
    job = root.after_calls[-1][0]
    window._quit()
    assert job in root.cancelled
    assert len(list(tmp_path.glob("bookmarks_*.md"))) == 1


def test_escape_must_be_pressed_twice_to_close(monkeypatch):
    from modules import subtitle_display

    window, drawn, _box = _overlay_window(monkeypatch)
    clock = {"now": 50.0}
    monkeypatch.setattr(subtitle_display.time, "monotonic", lambda: clock["now"])
    window._root.destroy = lambda: None

    window._request_quit()
    assert not window._stop.is_set()
    assert drawn[-1] == ("center", ["● 再按一次 Esc 關閉"])
    clock["now"] += 5.0  # too late: the prompt expired
    window._request_quit()
    assert not window._stop.is_set()
    clock["now"] += 0.5
    window._request_quit()
    assert window._stop.is_set()


def test_notice_expires_from_the_status_line(monkeypatch):
    from modules import subtitle_display

    window, drawn, _box = _overlay_window(monkeypatch)
    clock = {"now": 10.0}
    monkeypatch.setattr(subtitle_display.time, "monotonic", lambda: clock["now"])
    window._show("字幕")
    window._show_notice("★ 已標記")
    assert drawn[-1] == ("center", ["● ★ 已標記", "字幕"])
    clock["now"] += 3.0
    window._refresh_overlay(clock["now"])
    assert drawn[-1] == ("center", ["字幕"])


def test_failed_bookmark_write_is_kept_and_retried_on_close(monkeypatch, tmp_path):
    from modules import bookmarks, subtitle_display

    window, drawn, _box = _overlay_window(monkeypatch)
    root = window._root
    root.destroy = lambda: None
    from modules.live_status import SubtitleRecord

    clock = {"now": 100.0}
    monkeypatch.setattr(subtitle_display.time, "monotonic", lambda: clock["now"])
    records = [SubtitleRecord(99.0, "before", "前")]
    monkeypatch.setattr(subtitle_display.live_status, "recent_subtitles", lambda limit: list(records)[-limit:])
    real_write = bookmarks.write_text
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("file is open elsewhere")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(subtitle_display.bookmarks, "write_text", flaky)
    window._mark()
    records.append(SubtitleRecord(110.0, "follow", "後"))
    clock["now"] = 120.0
    root.after_calls[-1][2]()  # the 20 s timer fires and the write fails
    assert len(window._pending_marks) == 1
    assert "● 標記儲存失敗" in drawn[-1][1]
    records[:] = [SubtitleRecord(200.0, "much later", "很久以後")]  # history moved on before closing
    window._quit()
    text = next(tmp_path.glob("bookmarks_*.md")).read_text(encoding="utf-8")
    assert calls["n"] == 2 and "follow" in text and "much later" not in text
    assert window._stop.is_set()


def test_closing_survives_a_bookmark_that_cannot_be_written(monkeypatch):
    from modules import subtitle_display

    window, _drawn, _box = _overlay_window(monkeypatch)
    window._root.destroy = lambda: None
    monkeypatch.setattr(subtitle_display.bookmarks, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(UnicodeEncodeError("utf-8", "x", 0, 1, "bad")))
    window._mark()
    window._quit()
    assert window._stop.is_set() and window._pending_marks == []


def test_notices_show_while_history_is_open(monkeypatch):
    window, drawn, _box = _overlay_window(monkeypatch, records=[("안녕", "你好")])
    window._toggle_history()
    window._mark()
    assert drawn[-1] == ("left", ["● ★ 已標記", "안녕", "你好"])
    window._close_history()
    window._toggle_history()
    window._request_quit()
    assert drawn[-1][0] == "left" and drawn[-1][1][0] == "● 再按一次 Esc 關閉"
