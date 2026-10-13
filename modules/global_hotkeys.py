"""System-wide hotkeys (Windows ``RegisterHotKey``) so the subtitle window reacts without keyboard focus.

The window's own key bindings only fire while it has focus, and during a stream the focus is on the
player. A background thread registers the combinations and queues the action names; the Tk loop drains
them with :meth:`GlobalHotkeys.poll`, so every action still runs on the main thread.
"""

import queue
import sys
import threading

from utils.logger import get_logger

log = get_logger("global_hotkeys")

_MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
_MOD_NOREPEAT = 0x4000
_NAMED_KEYS = {"space": 0x20, **{f"f{n}": 0x6F + n for n in range(1, 13)}}
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


def parse_hotkey(combo: str) -> tuple[int, int] | None:
    """``"ctrl+alt+m"`` -> (modifier flags, virtual key); None for anything unusable.

    At least one modifier is required: a bare letter registered system-wide would swallow ordinary typing.
    """
    parts = [part.strip().lower() for part in (combo or "").split("+")]
    if len(parts) < 2 or not all(parts):
        return None
    *modifier_names, key = parts
    modifiers = 0
    for name in modifier_names:
        if name not in _MODIFIERS:
            return None
        modifiers |= _MODIFIERS[name]
    if key in _NAMED_KEYS:
        return modifiers, _NAMED_KEYS[key]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return modifiers, ord(key.upper())
    return None


class GlobalHotkeys:
    def __init__(self, bindings: dict[str, str]):
        """``bindings`` maps an action name to a combination such as ``"ctrl+alt+m"``; "" disables it."""
        self._bindings = {action: combo for action, combo in bindings.items() if combo}
        self._actions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._unavailable: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None or not self._bindings or sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, name="global-hotkeys", daemon=True)
        self._thread.start()

    def poll(self) -> list[str]:
        actions = []
        while True:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                return actions

    def unavailable(self) -> list[str]:
        """Combinations that could not be registered since the last call (taken elsewhere or unusable)."""
        combos = []
        while True:
            try:
                combos.append(self._unavailable.get_nowait())
            except queue.Empty:
                return combos

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._ready.wait(timeout=1.0)
        # Only a live thread still owns its id; Windows reuses the ids of finished threads.
        thread_id = self._thread_id
        if thread_id and thread.is_alive():
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
        thread.join(timeout=1.0)

    def _run(self) -> None:
        # RegisterHotKey delivers WM_HOTKEY to the registering thread, so register and pump here.
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        registered: dict[int, str] = {}
        try:
            for hotkey_id, (action, combo) in enumerate(self._bindings.items(), start=1):
                parsed = parse_hotkey(combo)
                if parsed is None:
                    log.warning("Global hotkey for %s ignored: cannot use %r", action, combo)
                    self._unavailable.put(combo)
                elif user32.RegisterHotKey(None, hotkey_id, parsed[0] | _MOD_NOREPEAT, parsed[1]):
                    registered[hotkey_id] = action
                else:
                    log.warning("Global hotkey %s for %s could not be registered (error %d; usually "
                                "another program holds it)", combo, action, ctypes.GetLastError())
                    self._unavailable.put(combo)
            if registered:
                log.info("Global hotkeys ready: %s", ", ".join(
                    f"{action}={self._bindings[action]}" for action in registered.values()))
            message = wintypes.MSG()
            # Peek once so the thread owns a message queue before stop() may post WM_QUIT to it.
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._ready.set()
            if not registered:
                return
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_HOTKEY and message.wParam in registered:
                    self._actions.put(registered[message.wParam])
        except Exception:  # hotkeys are a convenience; never take the pipeline down
            log.exception("Global hotkeys stopped")
        finally:
            self._thread_id = 0
            self._ready.set()
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
