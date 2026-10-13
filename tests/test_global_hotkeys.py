import queue
import sys
import threading
import time

import pytest

from modules.global_hotkeys import GlobalHotkeys, parse_hotkey
from modules.subtitle_display import SubtitleWindow


def test_parse_accepts_modifier_combinations_only():
    assert parse_hotkey("ctrl+alt+m") == (0x0003, ord("M"))
    assert parse_hotkey(" Ctrl + Shift + Space ") == (0x0006, 0x20)
    assert parse_hotkey("win+f9") == (0x0008, 0x78)
    assert parse_hotkey("alt+7") == (0x0001, ord("7"))
    for unusable in ("m", "", "ctrl+", "ctrl+alt", "hyper+m", "ctrl+escape", "ctrl+é", "ctrl++m"):
        assert parse_hotkey(unusable) is None, unusable


def test_disabled_or_empty_bindings_start_nothing():
    hotkeys = GlobalHotkeys({"bookmark": "", "pause": ""})
    hotkeys.start()
    assert hotkeys._thread is None and hotkeys.poll() == []
    hotkeys.stop()


class _StubHotkeys:
    def __init__(self, actions, unavailable=()):
        self._actions = list(actions)
        self._unavailable = list(unavailable)

    def poll(self):
        actions, self._actions = self._actions, []
        return actions

    def unavailable(self):
        combos, self._unavailable = self._unavailable, []
        return combos


def test_failing_action_does_not_stop_the_poll_loop_or_later_actions():
    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    scheduled = []
    window._root = type("Root", (), {"after": lambda self, ms, callback: scheduled.append(callback)})()
    window._refresh_overlay = lambda now: None
    calls = []

    def broken():
        raise RuntimeError("draw failed")

    window._toggle_history = broken
    window._mark = lambda: calls.append("bookmark")
    window._hotkeys = _StubHotkeys(["history", "bookmark"])
    window._poll()
    assert calls == ["bookmark"] and scheduled == [window._poll]


def test_unregistrable_combination_is_shown_on_the_status_line():
    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    notices = []
    window._show_notice = notices.append
    window._hotkeys = _StubHotkeys([], unavailable=["ctrl+alt+m"])
    window._run_hotkey_actions()
    window._run_hotkey_actions()
    assert notices == ["快捷鍵無法使用：ctrl+alt+m"]


@pytest.mark.skipif(sys.platform != "win32", reason="RegisterHotKey is Windows-only")
def test_nothing_registered_reports_and_stops_cleanly():
    hotkeys = GlobalHotkeys({"bookmark": "nonsense"})
    hotkeys.start()
    assert hotkeys._ready.wait(2.0)
    hotkeys._thread.join(2.0)
    assert hotkeys.unavailable() == ["nonsense"]
    assert hotkeys._thread_id == 0   # nothing left for stop() to post to
    hotkeys.stop()


def test_window_runs_queued_hotkey_actions_on_its_own_loop():
    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    calls = []
    window._mark = lambda: calls.append("bookmark")
    window._toggle_translation = lambda: calls.append("pause")
    window._toggle_history = lambda: calls.append("history")
    window._hotkeys = _StubHotkeys(["bookmark", "history", "pause"])
    window._poll()
    window._poll()
    assert calls == ["bookmark", "history", "pause"]


def test_hotkey_still_works_while_history_is_open():
    window = SubtitleWindow(queue.Queue(), threading.Event(), threading.Event())
    window._history_until = time.monotonic() + 60
    window._refresh_overlay = lambda now: None
    calls = []
    window._mark = lambda: calls.append("bookmark")
    window._hotkeys = _StubHotkeys(["bookmark"])
    window._poll()
    assert calls == ["bookmark"]


@pytest.mark.skipif(sys.platform != "win32", reason="RegisterHotKey is Windows-only")
def test_registered_combination_is_delivered_and_released():
    import ctypes

    combo = "ctrl+alt+shift+f12"
    hotkeys = GlobalHotkeys({"bookmark": combo, "broken": "nonsense"})
    hotkeys.start()
    assert hotkeys._ready.wait(2.0)
    # Deliver the message the system would send for the registered id.
    ctypes.windll.user32.PostThreadMessageW(hotkeys._thread_id, 0x0312, 1, 0)
    deadline = time.monotonic() + 2.0
    actions = []
    while not actions and time.monotonic() < deadline:
        actions = hotkeys.poll()
        time.sleep(0.01)
    hotkeys.stop()
    if "ctrl+alt+shift+f12" in hotkeys.unavailable():
        pytest.skip("another program on this desktop holds the test combination")
    assert actions == ["bookmark"]
    # After stop() the combination is free again for this (or any) thread.
    modifiers, key = parse_hotkey(combo)
    assert ctypes.windll.user32.RegisterHotKey(None, 99, modifiers, key)
    ctypes.windll.user32.UnregisterHotKey(None, 99)
