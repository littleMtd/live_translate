import queue
import threading

from modules.subtitle_display import SubtitleWindow


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
