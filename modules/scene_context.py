"""Automatic scene-context updater.

Samples the screen a few times per hour, asks a vision model one question —
"what game/activity is on screen" — and writes the short conclusion into
``cfg.translation.current_activity`` (picked up by the system prompt as a
labeled background line; see translator._compose_system_prompt).

Pollution guardrails, by construction:
- No screen TEXT is ever fed to the translator. Live chat / donations near
  the stream dominate on-screen text and are untrusted input; only the
  vision model's distilled conclusion (a short activity name) is used.
- The conclusion is sanitized (single line, length-capped) and "unknown"
  keeps the previous state instead of overwriting it.
- Every transition is emitted as a ``scene`` runtime event so QE scans can
  verify the context helps rather than pollutes.

Cost shape: a cheap 64x64 fingerprint gates the loop; vision calls happen at
most once per ``min_call_gap_sec`` and are forced at ``refresh_interval_sec``
even without a visible change — a handful of calls per hour.
"""

from __future__ import annotations

import base64
import ctypes
import io
import sys
import threading
import time
from ctypes import wintypes

from config import cfg
from utils.logger import get_logger
from utils.runtime_events import runtime_events

log = get_logger("scene_context")

_QUESTION = (
    "This is one frame from a Korean livestream. In at most five words, name "
    "the game or activity on screen (e.g. 'StarCraft', 'Minecraft', 'just "
    "chatting', 'watching a video'). Answer with only the name. If you are "
    "not sure, answer exactly: unknown"
)


def _frame_bytes(image) -> tuple[bytes, bytes]:
    """Returns (fingerprint_thumb_bytes, jpeg_bytes) for a PIL image."""
    thumb = image.convert("L").resize((64, 64)).tobytes()
    image.thumbnail((960, 960))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=70)
    return thumb, buffer.getvalue()


def _grab_primary_screen() -> tuple[bytes, bytes]:
    from PIL import ImageGrab

    image = ImageGrab.grab()
    return _frame_bytes(image)


def _find_window(title_keywords: tuple[str, ...]) -> tuple[int, tuple[int, int, int, int]] | None:
    """Return (hwnd, bbox) of the largest visible window matching a title keyword."""
    if sys.platform != "win32" or not title_keywords:
        return None

    keywords = tuple(keyword.lower() for keyword in title_keywords if keyword)
    if not keywords:
        return None

    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    matches: list[tuple[int, int, tuple[int, int, int, int], str]] = []

    def _window_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        title = _window_text(hwnd)
        if not title or not any(keyword in title.lower() for keyword in keywords):
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= 100 or height <= 100:
            return True
        matches.append((width * height, int(hwnd) if hwnd else 0, bbox, title))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not matches:
        return None
    _area, hwnd, bbox, title = max(matches, key=lambda item: item[0])
    log.debug("scene capture window matched: %r bbox=%s", title, bbox)
    return hwnd, bbox


def _print_window_image(hwnd: int, width: int, height: int):
    """Capture the window's OWN content via PrintWindow(PW_RENDERFULLCONTENT).

    A screen-region grab of the window's bbox photographs whatever is
    composited on top of it — with the stream behind an IDE the scene
    detector saw the IDE and answered "coding". PrintWindow asks the window
    itself to render, so an occluded/background stream still yields the
    stream. Returns None when the window refuses (caller falls back)."""
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    try:
        gdi32.SelectObject(mem_dc, bitmap)
        PW_RENDERFULLCONTENT = 0x00000002
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            return None

        class _BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        info = _BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height  # top-down rows
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0  # BI_RGB
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(mem_dc, bitmap, 0, height, pixels,
                           ctypes.byref(info), 0) != height:
            return None
        image = Image.frombuffer("RGB", (width, height), pixels, "raw", "BGRX", 0, 1)
        if image.convert("L").getextrema()[1] == 0:
            return None  # some GPU surfaces render black; let caller fall back
        return image
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def _grab_window_frame() -> tuple[bytes, bytes]:
    found = _find_window(tuple(getattr(cfg.scene, "window_title_keywords", ())))
    if found is None:
        if getattr(cfg.scene, "window_fallback_fullscreen", False):
            return _grab_primary_screen()
        raise RuntimeError("scene capture target window not found")
    hwnd, bbox = found
    image = _print_window_image(hwnd, bbox[2] - bbox[0], bbox[3] - bbox[1])
    if image is None:
        # Last resort: region grab (photographs whatever overlaps the bbox).
        from PIL import ImageGrab
        image = ImageGrab.grab(bbox=bbox)
    return _frame_bytes(image)


def _grab_frame() -> tuple[bytes, bytes]:
    mode = str(getattr(cfg.scene, "capture_mode", "primary_screen") or "primary_screen")
    if mode == "window":
        return _grab_window_frame()
    return _grab_primary_screen()


def _query_vision(jpeg: bytes) -> str:
    key = cfg.keys.groq or cfg.keys.groq_fallback
    if not key:
        raise RuntimeError("no groq key configured for scene vision")
    # Official SDK, same as the STT module: raw urllib gets Cloudflare-blocked
    # (403 error 1010) on Groq's API because of the default User-Agent.
    from groq import Groq

    client = Groq(api_key=key, timeout=cfg.scene.vision_timeout)
    response = client.chat.completions.create(
        model=cfg.scene.vision_model,
        max_tokens=30,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _QUESTION},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                }},
            ],
        }],
    )
    return str(response.choices[0].message.content or "")


def sanitize_activity(raw: str, max_chars: int) -> str:
    """One clean line or nothing. 'unknown' (and refusal-ish noise) → ''."""
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    line = line.strip().strip("\"'` .").strip()
    if not line or line.lower() == "unknown":
        return ""
    if len(line) > max_chars:  # a real name is short; long answers are noise
        return ""
    return line


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)


class SceneContextUpdater:
    def __init__(self, *, grab=_grab_frame, query=_query_vision, clock=time.monotonic):
        self._grab = grab
        self._query = query
        self._clock = clock
        self._prev_thumb: bytes | None = None
        self._last_call: float | None = None
        # A change observed while the call gap is still closed must not be
        # forgotten (the fingerprint absorbs it), or a game switch right
        # after a call would wait for the slow forced refresh.
        self._pending_change = False

    def tick(self) -> str | None:
        """Returns the new activity when the state changed, else None."""
        now = self._clock()
        thumb, jpeg = self._grab()
        if (self._prev_thumb is None
                or _mean_abs_diff(self._prev_thumb, thumb) >= cfg.scene.change_threshold):
            self._pending_change = True
        self._prev_thumb = thumb

        refresh_due = (self._last_call is None
                       or now - self._last_call >= cfg.scene.refresh_interval_sec)
        if not (self._pending_change or refresh_due):
            return None
        if self._last_call is not None and now - self._last_call < cfg.scene.min_call_gap_sec:
            return None
        self._pending_change = False
        self._last_call = now

        activity = sanitize_activity(self._query(jpeg), cfg.scene.max_activity_chars)
        if not activity:
            return None  # unknown/noise: keep the previous state
        current = getattr(cfg.translation, "current_activity", "") or ""
        if activity == current:
            return None
        object.__setattr__(cfg.translation, "current_activity", activity)
        log.info("scene: %r -> %r", current, activity)
        runtime_events.emit("scene", previous=current, activity=activity)
        return activity


def start(stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run() -> None:
        updater = SceneContextUpdater()
        log.info("Scene context updater started (model=%s)", cfg.scene.vision_model)
        while not stop_event.is_set():
            if pause_event is None or not pause_event.is_set():
                try:
                    updater.tick()
                except Exception as exc:  # never take the pipeline down
                    log.warning("scene tick failed: %s: %s", type(exc).__name__, exc)
            stop_event.wait(cfg.scene.check_interval_sec)

    thread = threading.Thread(target=run, name="scene_context", daemon=True)
    thread.start()
    return thread
