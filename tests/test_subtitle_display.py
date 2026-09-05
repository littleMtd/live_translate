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
