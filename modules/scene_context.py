"""Browser-window observation for streamer profile identity.

Observes exactly one explicitly platform-matching browser window and resolves
the broadcaster's profile from its calibrated identity block (or whole-scene
markers). The automatic activity (game) detector that also lived here was
switched off on 2026-10-11 and removed on 2026-11-10; manual
``cfg.translation.current_activity`` is the only activity source.

Privacy and capture boundaries:
- window-only capture for one validated HWND; never full-screen or bbox grabs;
- multiple candidate windows fail closed;
- titles and frames stay in memory and are never emitted;
- no provider/model fallback is selected implicitly.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import math
import os
import secrets
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol
from ctypes import wintypes

from config import cfg
from modules.activity_context import (
    capture_activity_snapshot,
    normalize_activity,
)
from modules.scene_vision import (
    VisionClassification,
    VisionDiagnostics,
    VisionProvider,
    VisionProviderFailure,
    build_vision_provider,
)
from modules.profile_context import (
    ContentProfileConsensus,
    ParsedProfileIdentity,
    build_profile_identity_prompt,
    parse_profile_identity_evidence,
    profile_resolution_status,
    profile_state,
)
from modules.identity_roi import (
    IDENTITY_ROI_PROMPT,
    IdentityRoiStore,
    calibration_key,
    crop_identity_roi,
    exact_reviewed_member,
    normalize_identity,
    parse_observed_identity,
)
from utils.logger import get_logger
from utils.runtime_events import runtime_events

log = get_logger("scene_context")

_CHROME_WINDOW_CLASS = "Chrome_WidgetWin_1"


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    class_name: str
    platform: str
    title: str
    bbox: tuple[int, int, int, int]
    lock_nonce: str = ""

    @property
    def stable_key(self) -> tuple[int, int, str, str]:
        return (self.hwnd, self.pid, self.class_name, self.platform)

    @property
    def lock_key(self) -> tuple[int, int, str, str, str]:
        return (*self.stable_key, self.title)


@dataclass(frozen=True)
class WindowResolution:
    status: str
    identity: WindowIdentity | None
    title_changed: bool = False
    matched_platform: str = ""
    ownership_changed: bool = False


@dataclass(frozen=True)
class CaptureFrame:
    status: str
    thumb: bytes = b""
    jpeg: bytes = b""
    fingerprint: bytes = b""
    frame_quality: str = ""
    content_crop: bool = False


class WindowCaptureBackend(Protocol):
    name: str

    def capture(self, identity: WindowIdentity) -> CaptureFrame:
        ...


def _user32():
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    return user32


def _window_text(user32, hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class(user32, hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(128)
    if not user32.GetClassNameW(hwnd, buffer, len(buffer)):
        return ""
    return buffer.value


def _window_pid(user32, hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_bbox(user32, hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    bbox = (rect.left, rect.top, rect.right, rect.bottom)
    if bbox[2] - bbox[0] <= 100 or bbox[3] - bbox[1] <= 100:
        return None
    return bbox


def _platform_for_title(title: str) -> str:
    folded = title.casefold()
    for keyword in cfg.scene.window_title_keywords:
        normalized = str(keyword or "").strip().casefold()
        if normalized and normalized in folded:
            return normalized
    return ""


def _browser_title_allowed(title: str) -> bool:
    configured = cfg.scene.browser_title_markers
    if configured is None:
        configured = (
            cfg.scene.chrome_title_marker,
        )
    markers = tuple(
        str(marker or "").strip().casefold()
        for marker in configured
        if str(marker or "").strip()
    )
    return not markers or any(marker in title.casefold() for marker in markers)


def _process_executable_name(pid: int) -> str:
    """Return a process basename without exposing its path to telemetry."""
    if sys.platform != "win32" or pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return ""
        return os.path.basename(buffer.value).casefold()
    finally:
        kernel32.CloseHandle(handle)


def _browser_process_allowed(pid: int) -> bool:
    configured = cfg.scene.browser_process_names
    allowed = {
        os.path.basename(str(name or "")).strip().casefold()
        for name in configured
        if str(name or "").strip()
    }
    return bool(allowed) and _process_executable_name(pid) in allowed


def _inspect_window(hwnd: int) -> WindowIdentity | None:
    """Read current identity. Numeric HWND reuse is rejected by other fields."""
    if sys.platform != "win32":
        return None
    user32 = _user32()
    if not user32.IsWindow(hwnd):
        return None
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    title = _window_text(user32, hwnd)
    class_name = _window_class(user32, hwnd)
    bbox = _window_bbox(user32, hwnd)
    platform = _platform_for_title(title)
    if not title or not class_name or bbox is None:
        return None
    if class_name != _CHROME_WINDOW_CLASS:
        return None
    pid = _window_pid(user32, hwnd)
    if not _browser_process_allowed(pid):
        return None
    return WindowIdentity(
        hwnd=int(hwnd),
        pid=pid,
        class_name=class_name,
        platform=platform,
        title=title,
        bbox=bbox,
    )


def _enum_platform_candidates() -> list[WindowIdentity]:
    """Enumerate only visible windows whose active title matches a platform."""
    if sys.platform != "win32":
        return []
    user32 = _user32()
    results: list[WindowIdentity] = []
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def enum_proc(hwnd, _lparam):
        identity = _inspect_window(int(hwnd) if hwnd else 0)
        if identity is not None:
            results.append(identity)
        return True

    user32.EnumWindows(enum_proc, 0)
    return results


def _diagnose_window_candidates() -> dict[str, int | str]:
    """Return bounded counters explaining an empty candidate set.

    Titles, HWNDs, process IDs, and geometry are intentionally excluded from
    telemetry.  The diagnostic is best-effort and never makes a window
    eligible; capture remains fail closed.
    """
    if sys.platform != "win32":
        return {"window_failure_reason": "unsupported_platform"}
    user32 = _user32()
    counts = {
        "platform_title_windows": 0,
        "supported_browser_windows": 0,
        "minimized_platform_windows": 0,
    }
    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    @enum_proc_type
    def enum_proc(hwnd, _lparam):
        title = _window_text(user32, int(hwnd) if hwnd else 0)
        if not title:
            return True
        platform = _platform_for_title(title)
        if platform:
            counts["platform_title_windows"] += 1
        class_name = _window_class(user32, int(hwnd) if hwnd else 0)
        supported = (
            class_name == _CHROME_WINDOW_CLASS
            and _browser_process_allowed(
                _window_pid(user32, int(hwnd) if hwnd else 0)
            )
        )
        if supported:
            counts["supported_browser_windows"] += 1
        if platform and supported and user32.IsIconic(hwnd):
            counts["minimized_platform_windows"] += 1
        return True

    user32.EnumWindows(enum_proc, 0)
    if counts["minimized_platform_windows"]:
        reason = "platform_window_minimized"
    elif counts["platform_title_windows"]:
        reason = "platform_window_not_capturable"
    elif counts["supported_browser_windows"]:
        reason = "platform_title_not_matched"
    else:
        reason = "supported_browser_window_not_found"
    return {"window_failure_reason": reason, **counts}


class SafeWindowResolver:
    """Session lock with fail-closed multi-window and identity validation."""

    def __init__(
        self,
        *,
        enumerate_windows: Callable[[], list[WindowIdentity]] = _enum_platform_candidates,
        inspect_window: Callable[[int], WindowIdentity | None] = _inspect_window,
        diagnose_windows: Callable[[], dict[str, int | str]] | None = None,
    ):
        self._enumerate = enumerate_windows
        self._inspect = inspect_window
        self._diagnose = diagnose_windows or (
            _diagnose_window_candidates
            if enumerate_windows is _enum_platform_candidates
            else lambda: {}
        )
        self._locked: WindowIdentity | None = None
        self.last_failure_diagnostics: dict[str, int | str] = {}
        self.window_generation = 0

    @property
    def locked_identity(self) -> WindowIdentity | None:
        return self._locked

    def _replace_lock(self, identity: WindowIdentity | None) -> None:
        old_key = self._locked.lock_key if self._locked else None
        new_key = identity.lock_key if identity else None
        if old_key != new_key:
            self.window_generation += 1
        self._locked = identity

    def resolve(self) -> WindowResolution:
        candidates = [
            candidate for candidate in self._enumerate() if candidate.platform
        ]
        if len(candidates) > 1:
            self.last_failure_diagnostics = {
                "window_failure_reason": "multiple_platform_windows",
                "eligible_platform_windows": len(candidates),
            }
            self._replace_lock(None)
            return WindowResolution("multiple_candidates", None)
        if not candidates:
            if self._locked is not None:
                current = self._inspect(self._locked.hwnd)
                if (
                    current is not None
                    and current.hwnd == self._locked.hwnd
                    and current.pid == self._locked.pid
                    and current.class_name == self._locked.class_name
                    and not current.platform
                ):
                    self.last_failure_diagnostics = {
                        "window_failure_reason": "player_not_visible"
                    }
                    return WindowResolution(
                        "player_not_visible",
                        None,
                        title_changed=True,
                    )
            self._replace_lock(None)
            try:
                self.last_failure_diagnostics = dict(self._diagnose())
            except Exception:
                self.last_failure_diagnostics = {
                    "window_failure_reason": "diagnostic_unavailable"
                }
            return WindowResolution("window_invalid", None)

        candidate = candidates[0]
        self.last_failure_diagnostics = {}
        ownership_changed = bool(
            self._locked
            and (
                self._locked.hwnd,
                self._locked.pid,
                self._locked.class_name,
            )
            != (candidate.hwnd, candidate.pid, candidate.class_name)
        )
        title_changed = bool(
            self._locked
            and self._locked.stable_key == candidate.stable_key
            and self._locked.title != candidate.title
        )
        if self._locked is None or self._locked.lock_key != candidate.lock_key:
            candidate = WindowIdentity(
                **{
                    **candidate.__dict__,
                    "lock_nonce": secrets.token_hex(8),
                }
            )
        else:
            candidate = WindowIdentity(
                **{
                    **candidate.__dict__,
                    "lock_nonce": self._locked.lock_nonce,
                }
            )
        self._replace_lock(candidate)
        return WindowResolution(
            "ok",
            candidate,
            title_changed=title_changed,
            matched_platform=candidate.platform,
            ownership_changed=ownership_changed,
        )

    def validate(self, expected: WindowIdentity) -> WindowResolution:
        candidates = [
            candidate for candidate in self._enumerate() if candidate.platform
        ]
        if len(candidates) > 1:
            self._replace_lock(None)
            return WindowResolution("multiple_candidates", None)
        if not candidates:
            current = self._inspect(expected.hwnd)
            if (
                current is not None
                and current.hwnd == expected.hwnd
                and current.pid == expected.pid
                and current.class_name == expected.class_name
                and not current.platform
            ):
                self.last_failure_diagnostics = {
                    "window_failure_reason": "player_not_visible"
                }
                return WindowResolution(
                    "player_not_visible",
                    None,
                    title_changed=True,
                )
            self._replace_lock(None)
            return WindowResolution("window_invalid", None)

        current = candidates[0]
        if (
            current.hwnd == expected.hwnd
            and current.pid == expected.pid
            and current.class_name == expected.class_name
            and not current.platform
        ):
            self._replace_lock(None)
            return WindowResolution(
                "wrong_tab",
                None,
                title_changed=True,
            )
        if current.stable_key != expected.stable_key:
            self._replace_lock(None)
            return WindowResolution("identity_changed", None)
        if current.title != expected.title:
            self._replace_lock(None)
            return WindowResolution(
                "title_changed",
                None,
                title_changed=True,
                matched_platform=current.platform,
            )
        current = WindowIdentity(
            **{
                **current.__dict__,
                "lock_nonce": expected.lock_nonce,
            }
        )
        self._locked = current
        return WindowResolution(
            "ok",
            current,
            title_changed=False,
            matched_platform=current.platform,
        )


def _print_window_image(hwnd: int, width: int, height: int):
    """Capture only the HWND's own surface. Failure has no screen fallback."""
    if sys.platform != "win32":
        return None
    from PIL import Image

    user32 = _user32()
    gdi32 = ctypes.windll.gdi32
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    previous_bitmap = None
    try:
        previous_bitmap = gdi32.SelectObject(mem_dc, bitmap)
        if not user32.PrintWindow(hwnd, mem_dc, 0x00000002):
            return None

        class _BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        info = _BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height
        info.biPlanes = 1
        info.biBitCount = 32
        pixels = ctypes.create_string_buffer(width * height * 4)
        if (
            gdi32.GetDIBits(
                mem_dc,
                bitmap,
                0,
                height,
                pixels,
                ctypes.byref(info),
                0,
            )
            != height
        ):
            return None
        return Image.frombuffer(
            "RGB", (width, height), pixels, "raw", "BGRX", 0, 1
        ).copy()
    finally:
        if previous_bitmap:
            gdi32.SelectObject(mem_dc, previous_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def _safe_player_crop(image):
    """Drop browser chrome, side chat, and edges before fingerprint/vision."""
    width, height = image.size
    left = int(width * 0.05)
    top = int(height * 0.18)
    right = max(left + 1, int(width * 0.78))
    bottom = max(top + 1, int(height * 0.92))
    return image.crop((left, top, right, bottom))


def _frame_quality(gray_thumb: bytes) -> str:
    if not gray_thumb:
        return "empty"
    count = len(gray_thumb)
    mean = sum(gray_thumb) / count
    variance = sum((value - mean) ** 2 for value in gray_thumb) / count
    black_ratio = sum(value <= 3 for value in gray_thumb) / count
    frequencies = Counter(gray_thumb)
    entropy = -sum(
        (amount / count) * math.log2(amount / count)
        for amount in frequencies.values()
    )
    if black_ratio >= 0.98 or mean <= 3:
        return "black"
    if variance < 4 or entropy < 0.35:
        return "low_variance"
    return "ok"


def _prepare_capture(image) -> CaptureFrame:
    crop = _safe_player_crop(image)
    thumb = crop.convert("L").resize((64, 64)).tobytes()
    quality = _frame_quality(thumb)
    if quality != "ok":
        return CaptureFrame(
            status="capture_low_quality",
            thumb=thumb,
            frame_quality=quality,
            content_crop=True,
        )
    export = crop.copy()
    export.thumbnail((960, 960))
    buffer = io.BytesIO()
    export.convert("RGB").save(buffer, "JPEG", quality=70)
    return CaptureFrame(
        status="ok",
        thumb=thumb,
        jpeg=buffer.getvalue(),
        fingerprint=hashlib.blake2s(thumb, digest_size=16).digest(),
        frame_quality=quality,
        content_crop=True,
    )


class PrintWindowCaptureBackend:
    name = "print_window"

    def capture(self, identity: WindowIdentity) -> CaptureFrame:
        width = identity.bbox[2] - identity.bbox[0]
        height = identity.bbox[3] - identity.bbox[1]
        image = _print_window_image(identity.hwnd, width, height)
        if image is None:
            return CaptureFrame(
                status="capture_unavailable",
                frame_quality="unavailable",
            )
        return _prepare_capture(image)


def _mean_significant_abs_diff(a: bytes, b: bytes, *, noise_floor: int = 3) -> float:
    """Measure structural luminance change without amplifying capture shimmer."""
    if not a or not b or len(a) != len(b):
        return float("inf")
    return sum(
        max(0, abs(x - y) - noise_floor)
        for x, y in zip(a, b)
    ) / len(a)


class SceneContextUpdater:
    """Generation-safe window observer that resolves the streamer profile."""

    def __init__(
        self,
        *,
        resolver: SafeWindowResolver | None = None,
        capture_backend: WindowCaptureBackend | None = None,
        profile_vision_provider: VisionProvider | None = None,
        identity_roi_provider: VisionProvider | None = None,
        identity_roi_store: IdentityRoiStore | None = None,
        profile_resolution_enabled: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
        event_sink: Callable[..., None] = runtime_events.emit,
        manual_activity_getter: Callable[[], object] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        pause_requested: Callable[[], bool] | None = None,
    ):
        self._resolver = resolver or SafeWindowResolver()
        self._capture = capture_backend or PrintWindowCaptureBackend()
        self._profile_enabled = (
            bool(profile_resolution_enabled)
            if profile_resolution_enabled is not None
            else bool(cfg.scene.resolve_content_profile)
        )
        self._profile_vision = profile_vision_provider
        self._profile_vision_injected = profile_vision_provider is not None
        self._identity_roi_vision = identity_roi_provider
        self._identity_roi_store = identity_roi_store or IdentityRoiStore()
        self._identity_roi_fingerprint: tuple[int, str, bytes] | None = None
        # Ignore per-pixel PrintWindow/JPEG shimmer while a localized name or
        # avatar change still forces a read before the stable polling deadline.
        self._identity_roi_change_threshold = 0.5
        self._identity_roi_active = False
        self._profile_registry_identity = profile_state.registry.identity
        if self._profile_enabled and self._profile_vision is None:
            self._profile_vision = build_vision_provider(
                build_profile_identity_prompt(profile_state.registry)
            )
        self._profile_consensus = ContentProfileConsensus()
        self._profile_next_call_at: float | None = None
        self._profile_fast_gap = float(
            cfg.scene.profile_identity_fast_call_gap_sec
        )
        self._profile_stable_gap = float(
            cfg.scene.profile_identity_stable_call_gap_sec
        )
        self._profile_confirmed_refresh = max(
            self._profile_stable_gap,
            float(cfg.scene.profile_identity_confirmed_refresh_sec),
        )
        # (window generation, calibration key, profile generation) whose ROI
        # identity is confirmed; while it matches and the name block is
        # unchanged, reads are deferred. Any profile-state change (registry
        # reload, manual/auto switch, clear) bumps the profile generation.
        self._identity_roi_confirmed_for: tuple[int, str, int] | None = None
        self._identity_roi_deferred_until = 0.0
        # (window generation, calibration key, monotonic time) of the last read that
        # a pixel-only change triggered and that re-confirmed the same profile.
        self._identity_roi_noisy_reconfirm: tuple[int, str, float] | None = None
        self._identity_roi_noisy_gap = max(
            0.0,
            float(getattr(cfg.scene, "profile_identity_noisy_reread_gap_sec", 0.0)),
        )
        self._profile_schema_retry_limit = int(
            cfg.scene.profile_identity_schema_retry_limit
        )
        self._profile_max_attempts_per_minute = int(
            cfg.scene.profile_identity_max_attempts_per_minute
        )
        self._profile_attempt_times: deque[float] = deque()
        self._profile_provider_failure_streak = 0
        self._profile_provider_backoff_until = 0.0
        self._profile_recovery_started_at: float | None = None
        # An unchanged name block that last read as nobody reviewed waits until
        # this time before it is read again; any visible change reads at once.
        self._identity_roi_unresolved_gap = float(
            cfg.scene.profile_identity_unresolved_reread_sec
        )
        self._identity_roi_unresolved_until = 0.0
        self._identity_roi_unresolved_reads = 0
        self._profile_expiry = float(
            cfg.scene.profile_identity_expiry_sec
        )
        self._profile_confirmed_at: float | None = None
        self._profile_observation_suspended_at: float | None = None
        self._profile_resolver_state = "startup"
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._event_sink = event_sink
        self._manual_getter = manual_activity_getter or (
            lambda: cfg.translation.current_activity
        )
        self._stop_requested = stop_requested
        self._pause_requested = pause_requested
        self.resolver_generation = 0
        self.effective_generation = 0
        self._manual_activity = normalize_activity(self._manual_getter())
        self._paused = False
        self._stopped = False
        self._last_window_status = ""
        self._seen_window_generation = -1
        self._last_effective_identity = self._effective_identity()
        self._sync_publication("startup", emit=False)

    @property
    def manual_activity(self) -> str:
        self._sync_manual_activity()
        return self._manual_activity

    def _effective_identity(self) -> tuple[str, str]:
        if self._manual_activity:
            snapshot = capture_activity_snapshot(
                self._manual_activity,
                source="manual",
            )
            return ("manual", snapshot.activity_id)
        return ("none", "")

    def _sync_publication(self, reason: str, *, emit: bool = True) -> None:
        """Track the effective activity identity (manual or none).

        Automatic activity is never published; the event keeps its historical
        fields so old and new runtime logs read the same way.
        """
        effective_identity = self._effective_identity()
        effective_changed = effective_identity != self._last_effective_identity
        if effective_changed:
            self.effective_generation += 1
            self._last_effective_identity = effective_identity
        if not emit or not effective_changed:
            return
        effective_source, effective_activity_id = effective_identity
        self._event_sink(
            "activity_publication",
            mode="record_only",
            publication_enabled=False,
            open_set_publication_enabled=bool(cfg.scene.publish_open_set_activity),
            action=(
                "manual_override" if effective_source == "manual" else "cleared"
            ),
            reason=reason,
            resolver_generation=self.resolver_generation,
            window_generation=self._resolver.window_generation,
            effective_generation=self.effective_generation,
            effective_source=effective_source,
            activity_id=effective_activity_id,
            activity_kind="",
            open_set_activity=False,
            automatic_available=False,
            manual_override_active=bool(self._manual_activity),
            translation_context_available=False,
            stt_terms_applied=False,
        )

    def _sync_manual_activity(self) -> None:
        current = normalize_activity(self._manual_getter())
        if current != self._manual_activity:
            self._manual_activity = current
            self._sync_publication("manual_activity_changed")

    def _sync_lifecycle(self) -> None:
        if self._stop_requested is not None and self._stop_requested():
            self.stop()
            return
        if self._pause_requested is not None:
            self.set_paused(self._pause_requested())

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused != self._paused:
            self._paused = paused
            self.resolver_generation += 1
            if paused:
                if self._profile_enabled:
                    self._profile_consensus.reset()
                    profile_state.clear_content("pipeline_paused")
            self._sync_publication(
                "pipeline_paused" if paused else "pipeline_resumed"
            )

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self.resolver_generation += 1
            if self._profile_enabled:
                self._profile_consensus.reset()
                profile_state.clear_content("pipeline_stopped")
            self._sync_publication("pipeline_stopped")

    def _emit(self, **fields) -> None:
        """Window/capture status event; the activity fields are fixed now."""
        safe_fields = {
            "mode": "record_only",
            "resolver_generation": self.resolver_generation,
            "window_generation": self._resolver.window_generation,
            "effective_generation": self.effective_generation,
            "manual_override_active": bool(self._manual_activity),
            "vision_provider": "",
            "vision_model": "",
            "published": False,
            "publication_blocked": True,
            "open_set_publication_enabled": bool(cfg.scene.publish_open_set_activity),
            "translation_context_applied": False,
            "stt_terms_applied": False,
            **fields,
        }
        self._event_sink("activity_shadow", **safe_fields)

    def _handle_invalid_window(self, resolution: WindowResolution) -> None:
        now = self._clock()
        previous_status = self._last_window_status
        if resolution.status in {"player_not_visible", "wrong_tab"}:
            self._mark_profile_observation_suspended(now)
            if resolution.status != previous_status:
                self._emit(
                    window_status="player_not_visible",
                    matched_platform=resolution.matched_platform,
                    title_match=False,
                    title_changed=resolution.title_changed,
                    capture_status="not_attempted",
                    discard_reason="observation_suspended",
                    **self._resolver.last_failure_diagnostics,
                )
            self._last_window_status = "player_not_visible"
            return
        self._mark_invalid_source(now, resolution.status)
        if resolution.status != previous_status:
            self._emit(
                window_status=resolution.status,
                matched_platform=resolution.matched_platform,
                title_match=False,
                title_changed=resolution.title_changed,
                capture_status="not_attempted",
                discard_reason=resolution.status,
                **self._resolver.last_failure_diagnostics,
            )
        self._last_window_status = resolution.status

    def _mark_profile_observation_suspended(self, now: float) -> None:
        """Retain confirmed content while its owning browser HWND is alive."""
        self._last_window_status = "player_not_visible"
        if not self._profile_enabled:
            return
        first_observation = self._profile_observation_suspended_at is None
        if first_observation:
            self._profile_observation_suspended_at = now
        self._profile_consensus.reset(self._resolver.window_generation)
        self._profile_next_call_at = None
        self._profile_resolver_state = "observation_suspended"
        self._profile_recovery_started_at = None
        if first_observation:
            prior_status = profile_resolution_status.current()
            self._emit_profile_resolution(
                status="suspended",
                reason="player_not_visible",
                last_detection_at=prior_status.get("profile_last_detection_at", ""),
                state_transition="visible_to_observation_suspended",
                activation_decision="retain_confirmed_profile",
                window_generation=self._resolver.window_generation,
                registry_generation=profile_state.registry.version,
                **profile_state.current().as_metadata(),
            )

    def _resume_profile_observation(self, now: float) -> None:
        suspended_at = self._profile_observation_suspended_at
        if suspended_at is None:
            return
        if self._profile_confirmed_at is not None:
            self._profile_confirmed_at += max(0.0, now - suspended_at)
        self._profile_observation_suspended_at = None
        self._profile_next_call_at = None
        self._profile_resolver_state = "resumed"

    def _mark_invalid_source(self, now: float, status: str) -> None:
        if status in {"player_not_visible", "wrong_tab"}:
            self._mark_profile_observation_suspended(now)
            return
        had_profile_content = bool(profile_state.current().content_profile_id)
        self._last_window_status = status
        if self._profile_enabled:
            # Observation loss can retire pending evidence, but it cannot
            # prove that the confirmed broadcaster changed.
            self._profile_consensus.reset(self._resolver.window_generation)
            self._profile_next_call_at = None
            self._profile_resolver_state = status
            if self._profile_recovery_started_at is None:
                self._profile_recovery_started_at = now
            self._profile_observation_suspended_at = None
            if had_profile_content:
                self._emit_profile_resolution(
                    status="suspended",
                    reason=status,
                    state_transition="window_to_observation_lost",
                    activation_decision="retain_confirmed_profile",
                    stale_profile_cleared=False,
                    window_generation=self._resolver.window_generation,
                    registry_generation=profile_state.registry.version,
                    **profile_state.current().as_metadata(),
                )
        self._sync_publication("invalid_window")

    def _clear_for_window_generation(
        self,
        reason: str,
    ) -> None:
        """Reset window-scoped evidence while retaining confirmed identity."""
        had_profile_content = bool(profile_state.current().content_profile_id)
        if self._profile_enabled:
            self._profile_consensus.reset(self._resolver.window_generation)
            self._profile_next_call_at = None
            self._profile_resolver_state = "revalidating_visible_player"
            self._profile_recovery_started_at = None
            self._profile_observation_suspended_at = None
            if had_profile_content:
                self._emit_profile_resolution(
                    status="revalidating",
                    reason=reason,
                    state_transition="window_generation_changed",
                    activation_decision="retain_confirmed_profile",
                    stale_profile_cleared=False,
                    window_generation=self._resolver.window_generation,
                    registry_generation=profile_state.registry.version,
                    **profile_state.current().as_metadata(),
                )
        self._sync_publication(reason)

    def _schedule_profile_resolution(
        self,
        now: float,
        state: str,
        *,
        stable: bool,
        gap: float | None = None,
    ) -> None:
        self._profile_resolver_state = state
        self._profile_next_call_at = now + (
            gap
            if gap is not None
            else self._profile_stable_gap if stable else self._profile_fast_gap
        )

    def _identity_roi_is_confirmed(self, window_generation: int, roi_key: str) -> bool:
        return self._identity_roi_confirmed_for == (
            window_generation,
            roi_key,
            profile_state.current().generation,
        )

    def _defer_identity_roi(
        self,
        now: float,
        window_generation: int,
        roi_key: str,
        retry_after: float | None = None,
    ) -> None:
        """Defer ROI re-reads while this window's identity is already confirmed.

        A misread, provider error or throttle on an unchanged name block keeps
        the confirmed profile, so retrying at the fast cadence only spends the
        vision quota shared with activity. This is ROI-local: the shared
        ``_profile_next_call_at`` keeps its cadence so the whole-scene resolver
        is unaffected if calibration disappears.
        """
        if self._identity_roi_is_confirmed(window_generation, roi_key):
            self._identity_roi_deferred_until = now + max(
                self._profile_confirmed_refresh, retry_after or 0.0
            )

    def _install_profile_provider_rate_limit_fence(
        self,
        diagnostics: VisionDiagnostics | None,
        *,
        now: float,
    ) -> float:
        """Install one provider-level fence from any rate-limited route attempt."""
        if diagnostics is None:
            return 0.0
        attempts = diagnostics.attempt_chain or ()
        rate_limited = [
            attempt
            for attempt in attempts
            if attempt.error_type == "rate_limit" or attempt.http_status == 429
        ]
        if not rate_limited and (
            diagnostics.error_type == "rate_limit"
            or diagnostics.http_status == 429
        ):
            reset_values = [diagnostics.rate_limit_reset_tokens_sec]
        else:
            reset_values = [
                attempt.rate_limit_reset_tokens_sec for attempt in rate_limited
            ]
        if not reset_values:
            return 0.0
        retry_after = max(
            self._profile_fast_gap,
            max(60.0 if reset is None else float(reset) for reset in reset_values)
            + 1.0,
        )
        self._profile_provider_backoff_until = max(
            self._profile_provider_backoff_until,
            now + retry_after,
        )
        return max(0.0, self._profile_provider_backoff_until - now)

    def _emit_profile_resolution(self, **fields: object) -> None:
        fields.setdefault("resolver_state", self._profile_resolver_state)
        fields.setdefault("last_detection_at", self._utc_now().isoformat())
        fields.setdefault("matched_markers", [])
        fields.setdefault("marker_strengths", [])
        profile_resolution_status.replace(**fields)
        self._event_sink("profile_resolution", **fields)

    def _reserve_profile_attempt(self, now: float, capacity: int) -> bool:
        cutoff = now - 60.0
        while self._profile_attempt_times and self._profile_attempt_times[0] <= cutoff:
            self._profile_attempt_times.popleft()
        if len(self._profile_attempt_times) + capacity > self._profile_max_attempts_per_minute:
            return False
        self._profile_attempt_times.extend(now for _ in range(capacity))
        return True

    def _release_unused_profile_attempts(self, reserved: int, used: int) -> None:
        for _ in range(max(0, reserved - used)):
            self._profile_attempt_times.pop()

    def _enter_profile_recovery(
        self,
        now: float,
        state: str,
        *,
        reason: str,
        gap: float | None = None,
    ) -> bool:
        if self._profile_recovery_started_at is None:
            self._profile_recovery_started_at = now
        self._schedule_profile_resolution(now, state, stable=False, gap=gap)
        return False

    def _leave_profile_recovery(self) -> None:
        self._profile_recovery_started_at = None
        self._profile_provider_failure_streak = 0

    def _expire_profile_if_needed(self, now: float) -> None:
        if (
            not self._profile_enabled
            or self._identity_roi_active
            or self._profile_confirmed_at is None
            or self._profile_observation_suspended_at is not None
            or now - self._profile_confirmed_at < self._profile_expiry
        ):
            return
        self._profile_confirmed_at = None
        self._profile_consensus.reset(self._resolver.window_generation)
        self._profile_next_call_at = None
        self._profile_resolver_state = "expired"
        self._profile_recovery_started_at = now

    def _resolve_content_profile(
        self,
        frame: CaptureFrame,
        identity: WindowIdentity,
        *,
        now: float,
        resolver_generation: int,
        window_generation: int,
    ) -> None:
        if not self._profile_enabled or self._profile_vision is None:
            return
        if not frame.content_crop:
            return
        if profile_state.current().mode == "manual":
            self._profile_consensus.reset(window_generation)
            return
        registry = profile_state.registry
        if registry.identity != self._profile_registry_identity:
            self._profile_registry_identity = registry.identity
            self._profile_consensus.reset(window_generation)
            self._profile_confirmed_at = None
            profile_state.clear_content("profile_registry_changed")
            self._profile_next_call_at = None
            self._profile_resolver_state = "registry_changed"
            self._profile_recovery_started_at = None
            if not self._profile_vision_injected:
                self._profile_vision = build_vision_provider(
                    build_profile_identity_prompt(registry)
                )
        roi = self._identity_roi_store.get(calibration_key(identity.platform))
        if now < self._profile_provider_backoff_until:
            return
        if roi is not None:
            self._identity_roi_active = True
            self._resolve_authoritative_identity_roi(
                frame,
                identity,
                roi=roi,
                now=now,
                resolver_generation=resolver_generation,
                window_generation=window_generation,
            )
            return
        self._identity_roi_active = False
        starting_snapshot = profile_state.current()
        self._expire_profile_if_needed(now)
        if self._profile_next_call_at is not None and now < self._profile_next_call_at:
            return
        validation = self._resolver.validate(identity)
        discard = self._window_discard_reason(
            validation,
            resolver_generation=resolver_generation,
            window_generation=window_generation,
        )
        if discard:
            self._profile_consensus.reset(window_generation)
            return
        started = self._clock()
        request_started_at = self._utc_now().isoformat()
        retry_count = 0
        request_diagnostics: list[VisionDiagnostics] = []
        result: str | VisionClassification
        while True:
            attempt_now = self._clock()
            route_capacity = max(
                1,
                len(getattr(self._profile_vision, "route_identities", ())),
            )
            if not self._reserve_profile_attempt(attempt_now, route_capacity):
                self._schedule_profile_resolution(now, "rate_limited", stable=False)
                self._emit_profile_resolution(
                    status="throttled",
                    reason="profile_attempt_budget",
                    resolver_state=self._profile_resolver_state,
                    state_transition="request_to_rate_limited",
                    request_started_at=request_started_at,
                    schema_retry_count=retry_count,
                    profile_attempts_last_minute=len(self._profile_attempt_times),
                    window_generation=window_generation,
                    registry_generation=registry.version,
                    **profile_state.current().as_metadata(),
                )
                return
            try:
                result = self._profile_vision.classify(frame.jpeg)
            except Exception as exc:
                failure_diagnostics = (
                    exc.diagnostics
                    if isinstance(exc, VisionProviderFailure)
                    else None
                )
                used_attempts = (
                    max(1, len(failure_diagnostics.attempt_chain))
                    if failure_diagnostics is not None
                    else 1
                )
                self._release_unused_profile_attempts(route_capacity, used_attempts)
                diagnostics = (
                    failure_diagnostics.event_fields()
                    if failure_diagnostics is not None
                    else {
                        "vision_outcome": "error",
                        "vision_error_type": "provider_error",
                    }
                )
                self._profile_provider_failure_streak += 1
                cooldown = min(
                    self._profile_fast_gap
                    * (2 ** (self._profile_provider_failure_streak - 1)),
                    30.0,
                )
                retry_after = self._install_profile_provider_rate_limit_fence(
                    failure_diagnostics,
                    now=self._clock(),
                )
                cooldown = max(cooldown, retry_after)
                cleared = self._enter_profile_recovery(
                    now,
                    "provider_error",
                    reason="profile_provider_unavailable",
                    gap=cooldown,
                )
                self._emit_profile_resolution(
                    status="provider_error",
                    reason=type(exc).__name__,
                    parser_rejection_reason="",
                    resolver_state=self._profile_resolver_state,
                    state_transition="request_to_provider_error",
                    request_started_at=request_started_at,
                    schema_retry_count=retry_count,
                    provider_failure_streak=self._profile_provider_failure_streak,
                    recovery_cooldown_sec=cooldown,
                    stale_profile_cleared=cleared,
                    window_generation=window_generation,
                    registry_generation=registry.version,
                    latency_ms=round((self._clock() - started) * 1000, 2),
                    **diagnostics,
                    **profile_state.current().as_metadata(),
                )
                return
            if isinstance(result, VisionClassification):
                request_diagnostics.append(result.diagnostics)
                self._install_profile_provider_rate_limit_fence(
                    result.diagnostics,
                    now=self._clock(),
                )
                self._release_unused_profile_attempts(
                    route_capacity,
                    max(1, len(result.diagnostics.attempt_chain)),
                )
                raw = result.text
            else:
                self._release_unused_profile_attempts(route_capacity, 1)
                raw = result
            self._profile_provider_failure_streak = 0
            parsed = parse_profile_identity_evidence(raw, registry)
            if not (
                parsed.status == "rejected"
                and parsed.rejection_reason in {
                    "invalid_response_type",
                    "invalid_json",
                    "invalid_schema",
                }
                and retry_count < self._profile_schema_retry_limit
                and self._clock() >= self._profile_provider_backoff_until
            ):
                break
            validation = self._resolver.validate(identity)
            retry_discard = self._window_discard_reason(
                validation,
                resolver_generation=resolver_generation,
                window_generation=window_generation,
            )
            current_snapshot = profile_state.current()
            if (
                retry_discard
                or profile_state.registry.identity != registry.identity
                or current_snapshot.generation != starting_snapshot.generation
            ):
                parsed = ParsedProfileIdentity(
                    "rejected",
                    rejection_reason="stale_before_schema_retry",
                )
                break
            retry_count += 1
        vision_diagnostics = (
            request_diagnostics[-1].event_fields()
            if request_diagnostics else {}
        )
        total_costs = [
            attempt.api_cost_usd
            for diagnostics in request_diagnostics
            for attempt in diagnostics.attempt_chain
            if attempt.api_cost_usd is not None
        ]
        request_fields = {
            "request_started_at": request_started_at,
            "schema_retry_count": retry_count,
            "profile_request_count": len(request_diagnostics) or 1,
            "profile_route_attempt_count": sum(
                max(1, len(item.attempt_chain)) for item in request_diagnostics
            ) or 1,
            "profile_attempts_last_minute": len(self._profile_attempt_times),
            "registry_generation": registry.version,
            "parser_rejection_reason": parsed.rejection_reason,
            "matched_markers": list(parsed.matched_markers),
            "marker_strengths": list(parsed.marker_strengths),
            "marker_evidence_strength": parsed.evidence_strength,
            "profile_request_total_cost_usd": (
                round(sum(total_costs), 10) if total_costs else None
            ),
            **vision_diagnostics,
        }
        validation = self._resolver.validate(identity)
        discard = self._window_discard_reason(
            validation,
            resolver_generation=resolver_generation,
            window_generation=window_generation,
        )
        if discard:
            self._profile_consensus.reset(window_generation)
            self._schedule_profile_resolution(now, "discarded", stable=False)
            self._emit_profile_resolution(
                status="discarded",
                reason=discard,
                state_transition="request_to_discarded",
                window_generation=window_generation,
                latency_ms=round((self._clock() - started) * 1000, 2),
                **request_fields,
                **profile_state.current().as_metadata(),
            )
            return
        # Parse against the exact registry generation used to construct this
        # request. A concurrent reload makes the result stale, never salvageable.
        current_snapshot = profile_state.current()
        if (
            profile_state.registry.identity != registry.identity
            or current_snapshot.generation != starting_snapshot.generation
        ):
            self._profile_consensus.reset(window_generation)
            self._schedule_profile_resolution(now, "discarded", stable=False)
            self._emit_profile_resolution(
                status="discarded",
                reason="profile_generation_changed",
                state_transition="request_to_generation_discard",
                window_generation=window_generation,
                latency_ms=round((self._clock() - started) * 1000, 2),
                **request_fields,
                **profile_state.current().as_metadata(),
            )
            return
        status, candidate = parsed.status, parsed.profile_id
        if status == "unknown":
            self._profile_consensus.reset(window_generation)
            cleared = self._enter_profile_recovery(
                now,
                "seeking",
                reason="profile_unknown_timeout",
            )
            self._emit_profile_resolution(
                status="unknown",
                candidate_profile_id="",
                candidate_streak=0,
                resolver_state=self._profile_resolver_state,
                state_transition="request_to_unknown",
                stale_profile_cleared=cleared,
                window_generation=window_generation,
                latency_ms=round((self._clock() - started) * 1000, 2),
                **request_fields,
                **profile_state.current().as_metadata(),
            )
            return
        if status != "accepted":
            self._profile_consensus.reset(window_generation)
            cleared = self._enter_profile_recovery(
                now,
                "conflict" if status == "conflict" else "recovering",
                reason=(
                    "profile_conflict_timeout"
                    if status == "conflict"
                    else "profile_schema_timeout"
                ),
            )
            self._emit_profile_resolution(
                status="rejected",
                reason=(
                    "conflicting_identity_markers"
                    if status == "conflict"
                    else "invalid_schema_or_allowlist"
                ),
                stale_profile_cleared=cleared,
                candidate_profile_id="",
                candidate_streak=0,
                resolver_state=self._profile_resolver_state,
                state_transition=(
                    "request_to_conflict"
                    if status == "conflict"
                    else "request_to_schema_rejection"
                ),
                window_generation=window_generation,
                latency_ms=round((self._clock() - started) * 1000, 2),
                **request_fields,
                **profile_state.current().as_metadata(),
            )
            return
        self._leave_profile_recovery()
        current_effective = profile_state.current().effective_profile_id
        if parsed.strong and candidate == current_effective:
            self._profile_consensus.reset(window_generation)
            profile_state.confirm_content(
                candidate,
                confidence=1.0,
                evidence_source="scene_exact_member_marker",
            )
            self._profile_confirmed_at = now
            self._schedule_profile_resolution(now, "stable", stable=True)
            self._emit_profile_resolution(
                status="confirmed",
                reason="exact_member_marker",
                candidate_profile_id=candidate,
                candidate_streak=1,
                strong_identity_evidence=True,
                immediate_activation_eligible=True,
                resolver_state=self._profile_resolver_state,
                state_transition="strong_marker_to_stable",
                previous_effective_profile_id=current_effective,
                new_effective_profile_id=profile_state.current().effective_profile_id,
                activation_decision="immediate_strong_marker",
                distinct_frame=True,
                window_generation=window_generation,
                latency_ms=round((self._clock() - started) * 1000, 2),
                **request_fields,
                **profile_state.current().as_metadata(),
            )
            return
        matched_marker_records = tuple(
            marker
            for marker_id in parsed.matched_markers
            if (marker := registry.marker(marker_id)) is not None
        )
        member_marker_count = sum(
            marker.kind == "member_name" for marker in matched_marker_records
        )
        has_group_brand = any(
            marker.kind == "group_brand" for marker in matched_marker_records
        )
        # A member name can be a guest, roster, chat/donation name, or text in a
        # reaction clip. It is useful candidate evidence but cannot independently
        # authorize a cross-profile mutation. Corroboration comes from persistent
        # layout/appearance (no member marker) or one member plus its group brand;
        # a multi-member roster is deliberately not owner evidence.
        profile_corroborated = (
            member_marker_count == 0
            or (member_marker_count == 1 and has_group_brand)
        )
        frame_key = hashlib.sha256(frame.thumb or frame.jpeg).hexdigest()[:16]
        streak, confirmed, conflict, evidence_added = self._profile_consensus.observe(
            candidate,
            frame_key=frame_key,
            window_generation=window_generation,
            profile_corroborated=profile_corroborated,
        )
        consensus_corroborated = self._profile_consensus.profile_corroborated
        if confirmed:
            profile_state.confirm_content(candidate, confidence=1.0)
            self._profile_confirmed_at = now
        self._schedule_profile_resolution(
            now,
            "stable" if confirmed else "candidate",
            stable=confirmed,
        )
        self._emit_profile_resolution(
            status="confirmed" if confirmed else "candidate",
            reason="conflict_reset" if conflict else "",
            candidate_profile_id=candidate,
            candidate_streak=streak,
            strong_identity_evidence=parsed.strong,
            immediate_activation_eligible=False,
            member_marker_count=member_marker_count,
            profile_corroborated=consensus_corroborated,
            resolver_state=self._profile_resolver_state,
            state_transition=(
                "cross_profile_consensus_to_stable"
                if confirmed else "request_to_candidate"
            ),
            activation_decision=(
                "cross_profile_consensus_confirmed"
                if confirmed
                else (
                    "awaiting_profile_corroboration"
                    if streak >= 2 and not consensus_corroborated
                    else "awaiting_cross_profile_consensus"
                )
            ),
            distinct_frame=evidence_added,
            window_generation=window_generation,
            latency_ms=round((self._clock() - started) * 1000, 2),
            **request_fields,
            **profile_state.current().as_metadata(),
        )

    def _resolve_authoritative_identity_roi(
        self,
        frame: CaptureFrame,
        identity: WindowIdentity,
        *,
        roi,
        now: float,
        resolver_generation: int,
        window_generation: int,
    ) -> None:
        """Use a calibrated name block as the sole automatic profile authority."""
        roi_key = calibration_key(identity.platform)
        observation = crop_identity_roi(frame.jpeg, roi)
        roi_available = observation is not None
        previous = self._identity_roi_fingerprint
        roi_changed = bool(
            observation is not None
            and (
                previous is None
                or previous[:2] != (window_generation, roi_key)
                or _mean_significant_abs_diff(previous[2], observation.thumb)
                >= self._identity_roi_change_threshold
            )
        )
        if observation is None:
            self._schedule_profile_resolution(now, "identity_roi_unavailable", stable=False)
            self._emit_profile_resolution(
                status="unavailable",
                reason="identity_roi_crop_unavailable",
                activation_decision="retain_confirmed_profile",
                identity_roi_calibration_key=roi_key,
                identity_roi=roi.as_dict(),
                identity_roi_available=False,
                identity_roi_changed=False,
                identity_read_attempted=False,
                normalized_observed_identity="",
                reviewed_member_match="",
                window_generation=window_generation,
                **profile_state.current().as_metadata(),
            )
            return
        pixel_only_change = bool(
            roi_changed
            and previous is not None
            and previous[:2] == (window_generation, roi_key)
        )
        noisy = self._identity_roi_noisy_reconfirm
        if (
            pixel_only_change
            and noisy is not None
            and noisy[:2] == (window_generation, roi_key)
            and now - noisy[2] < self._identity_roi_noisy_gap
            and self._identity_roi_is_confirmed(window_generation, roi_key)
        ):
            # This block already "changed" without changing identity; keep the
            # confirmed profile and wait instead of reading at the fast cadence.
            return
        if roi_changed:
            # A visibly different name block (or a new window) needs a fresh,
            # fast identity read; confirmation no longer applies.
            self._identity_roi_confirmed_for = None
            self._identity_roi_unresolved_until = 0.0
            self._identity_roi_unresolved_reads = 0
        elif now < self._identity_roi_unresolved_until:
            # The same unrecognized block again: reading it every few seconds only
            # spent the vision quota that the next real stream switch needs.
            return
        due = self._profile_next_call_at is None or now >= self._profile_next_call_at
        if not roi_changed and not due:
            return
        if (
            not roi_changed
            and self._identity_roi_is_confirmed(window_generation, roi_key)
            and now < self._identity_roi_deferred_until
        ):
            return
        self._identity_roi_fingerprint = (
            window_generation,
            roi_key,
            observation.thumb,
        )
        if now < self._profile_provider_backoff_until:
            return
        validation = self._resolver.validate(identity)
        discard = self._window_discard_reason(
            validation,
            resolver_generation=resolver_generation,
            window_generation=window_generation,
        )
        if discard:
            self._schedule_profile_resolution(now, "discarded", stable=False)
            self._defer_identity_roi(now, window_generation, roi_key)
            self._emit_profile_resolution(
                status="discarded",
                reason=discard,
                activation_decision="retain_confirmed_profile",
                identity_roi_calibration_key=roi_key,
                identity_roi=roi.as_dict(),
                identity_roi_available=roi_available,
                identity_roi_changed=roi_changed,
                identity_read_attempted=False,
                normalized_observed_identity="",
                reviewed_member_match="",
                window_generation=window_generation,
                **profile_state.current().as_metadata(),
            )
            return
        if self._identity_roi_vision is None:
            self._identity_roi_vision = build_vision_provider(IDENTITY_ROI_PROMPT)
        started = self._clock()
        starting_snapshot = profile_state.current()
        route_capacity = max(
            1, len(getattr(self._identity_roi_vision, "route_identities", ()))
        )
        if not self._reserve_profile_attempt(now, route_capacity):
            self._schedule_profile_resolution(now, "rate_limited", stable=False)
            self._defer_identity_roi(now, window_generation, roi_key)
            self._emit_profile_resolution(
                status="throttled",
                reason="profile_attempt_budget",
                activation_decision="retain_confirmed_profile",
                identity_authority="calibrated_channel_identity_roi",
                identity_roi_calibration_key=roi_key,
                identity_roi=roi.as_dict(),
                identity_roi_available=True,
                identity_roi_changed=roi_changed,
                identity_read_attempted=False,
                normalized_observed_identity="",
                reviewed_member_match="",
                profile_attempts_last_minute=len(self._profile_attempt_times),
                window_generation=window_generation,
                **profile_state.current().as_metadata(),
            )
            return
        try:
            result = self._identity_roi_vision.classify(observation.jpeg)
            raw = result.text if isinstance(result, VisionClassification) else result
        except Exception as exc:
            failure_now = self._clock()
            diagnostics = exc.diagnostics if isinstance(exc, VisionProviderFailure) else None
            diagnostic_fields = (
                diagnostics.event_fields()
                if diagnostics is not None
                else {
                    "vision_outcome": "error",
                    "vision_error_type": "provider_error",
                }
            )
            used_attempts = max(1, len(diagnostics.attempt_chain)) if diagnostics is not None else 1
            self._release_unused_profile_attempts(route_capacity, used_attempts)
            retry_after = self._install_profile_provider_rate_limit_fence(
                diagnostics,
                now=failure_now,
            )
            self._schedule_profile_resolution(
                failure_now,
                "identity_roi_provider_error",
                stable=False,
                gap=retry_after or None,
            )
            self._defer_identity_roi(
                failure_now, window_generation, roi_key, retry_after
            )
            self._emit_profile_resolution(
                status="provider_error",
                reason=type(exc).__name__,
                activation_decision="retain_confirmed_profile",
                identity_roi_calibration_key=roi_key,
                identity_roi=roi.as_dict(),
                identity_roi_available=True,
                identity_roi_changed=roi_changed,
                identity_read_attempted=True,
                normalized_observed_identity="",
                reviewed_member_match="",
                latency_ms=round((self._clock() - started) * 1000, 2),
                profile_provider_retry_after_sec=(
                    round(retry_after, 3) if retry_after else None
                ),
                window_generation=window_generation,
                **diagnostic_fields,
                **profile_state.current().as_metadata(),
            )
            return
        used_attempts = (
            max(1, len(result.diagnostics.attempt_chain))
            if isinstance(result, VisionClassification)
            else 1
        )
        self._release_unused_profile_attempts(route_capacity, used_attempts)
        # A read that succeeded, even on the fallback-key route after the primary key
        # hit 429, leaves no fence: the next name read must not wait ~45 s while the
        # fallback key still has quota. A read where every route failed still fences.
        self._profile_provider_backoff_until = 0.0
        discard = self._window_discard_reason(
            self._resolver.validate(identity),
            resolver_generation=resolver_generation,
            window_generation=window_generation,
        )
        if (
            not discard
            and profile_state.current().generation != starting_snapshot.generation
        ):
            discard = "profile_generation_changed"
        observed = parse_observed_identity(raw)
        normalized = normalize_identity(observed)
        marker = (
            None
            if discard
            else exact_reviewed_member(observed, profile_state.registry)
        )
        if discard:
            normalized = ""
        if discard:
            decision = "discard_stale_identity_read"
            status = "discarded"
            reason = discard
        elif marker is None:
            # Nobody reviewed: keep the current profile (owner decision 2026-10-16). A
            # misread or page overlay ("orts" during a 마냥 stream, run 20261013) must not
            # drop it; an unlisted streamer keeps the previous one until a listed name shows.
            decision = "retain_confirmed_profile"
            status = "unknown"
            reason = "identity_blank_or_not_reviewed"
        else:
            profile_state.confirm_content(
                marker.profile_id,
                confidence=1.0,
                evidence_source="authoritative_identity_roi",
            )
            self._profile_confirmed_at = now
            decision = "authoritative_identity_confirmed"
            status = "confirmed"
            reason = "exact_reviewed_member_name"
        if status == "unknown":
            # 30 s, then 60, 120, 300 s while the same block stays unrecognized, so an
            # unlisted streamer or a lasting overlay does not hold half the quota for good.
            self._identity_roi_unresolved_reads += 1
            self._identity_roi_unresolved_until = now + min(
                300.0,
                self._identity_roi_unresolved_gap * 2 ** (self._identity_roi_unresolved_reads - 1),
            )
        else:
            self._identity_roi_unresolved_until = 0.0
            self._identity_roi_unresolved_reads = 0
        confirmed_read = status == "confirmed"
        if confirmed_read:
            self._identity_roi_confirmed_for = (
                window_generation,
                roi_key,
                profile_state.current().generation,
            )
            if (
                pixel_only_change
                and profile_state.current().effective_profile_id
                == starting_snapshot.effective_profile_id
            ):
                self._identity_roi_noisy_reconfirm = (window_generation, roi_key, now)
            else:
                self._identity_roi_noisy_reconfirm = None
        self._schedule_profile_resolution(
            now,
            "stable" if confirmed_read else "seeking",
            stable=confirmed_read,
        )
        self._defer_identity_roi(now, window_generation, roi_key)
        self._emit_profile_resolution(
            status=status,
            reason=reason,
            activation_decision=decision,
            identity_authority="calibrated_channel_identity_roi",
            identity_roi_calibration_key=roi_key,
            identity_roi=roi.as_dict(),
            identity_roi_available=True,
            identity_roi_changed=roi_changed,
            identity_read_attempted=True,
            normalized_observed_identity=normalized,
            reviewed_member_match=marker.marker_id if marker is not None else "",
            candidate_profile_id=marker.profile_id if marker is not None else "",
            latency_ms=round((self._clock() - started) * 1000, 2),
            window_generation=window_generation,
            # which route answered, and any 429 before it, so a run shows the fallback key at work
            **(result.diagnostics.event_fields() if isinstance(result, VisionClassification) else {}),
            **profile_state.current().as_metadata(),
        )

    def _window_discard_reason(
        self,
        validation: WindowResolution,
        *,
        resolver_generation: int,
        window_generation: int,
    ) -> str:
        if self._stopped:
            return "pipeline_stopped"
        if self._paused:
            return "pipeline_paused"
        if self.resolver_generation != resolver_generation:
            return "resolver_generation_changed"
        if validation.status in {
            "player_not_visible",
            "wrong_tab",
            "window_invalid",
            "multiple_candidates",
        }:
            return validation.status
        if self._resolver.window_generation != window_generation:
            return "window_generation_changed"
        if validation.status != "ok":
            return (
                "window_generation_changed"
                if validation.status in {"identity_changed", "title_changed"}
                else validation.status
            )
        return ""

    def tick(self) -> None:
        self._sync_lifecycle()
        self._sync_manual_activity()
        if self._stopped or self._paused:
            return None
        capture_mode = str(
            cfg.scene.capture_mode or ""
        )
        if capture_mode not in {"chrome_window", "window"}:
            self._emit(
                window_status="unsupported_capture_mode",
                capture_status="not_attempted",
                discard_reason="unsupported_capture_mode",
            )
            return None

        resolution = self._resolver.resolve()
        if resolution.status != "ok" or resolution.identity is None:
            self._handle_invalid_window(resolution)
            return None
        self._identity_roi_active = self._identity_roi_store.get(
            calibration_key(resolution.identity.platform)
        ) is not None
        self._resume_profile_observation(self._clock())
        self._expire_profile_if_needed(self._clock())
        self._last_window_status = "ok"
        if self._resolver.window_generation != self._seen_window_generation:
            self._clear_for_window_generation(
                "window_generation_changed",
            )
            self._seen_window_generation = self._resolver.window_generation
        identity = resolution.identity
        capture_resolver_generation = self.resolver_generation
        capture_window_generation = self._resolver.window_generation
        frame = self._capture.capture(identity)
        if frame.status != "ok":
            if self._profile_enabled:
                if self._identity_roi_active:
                    cleared = False
                    self._schedule_profile_resolution(
                        self._clock(), "identity_roi_capture_unavailable", stable=False
                    )
                else:
                    cleared = self._enter_profile_recovery(
                        self._clock(),
                        "capture_failure",
                        reason="profile_capture_timeout",
                    )
                self._emit_profile_resolution(
                    status="capture_failure",
                    reason=frame.status,
                    state_transition="capture_to_recovery",
                    activation_decision=(
                        "fallback_to_source"
                        if cleared
                        else "retain_confirmed_profile"
                        if self._identity_roi_active
                        else "retain_during_grace"
                    ),
                    identity_authority=(
                        "calibrated_channel_identity_roi"
                        if self._identity_roi_active
                        else "whole_scene"
                    ),
                    identity_roi_calibration_key=(
                        calibration_key(identity.platform)
                        if self._identity_roi_active
                        else ""
                    ),
                    identity_roi_available=False,
                    identity_roi_changed=False,
                    identity_read_attempted=False,
                    stale_profile_cleared=cleared,
                    window_generation=capture_window_generation,
                    registry_generation=profile_state.registry.version,
                    **profile_state.current().as_metadata(),
                )
            self._emit(
                window_status="ok",
                matched_platform=identity.platform,
                title_match=True,
                title_changed=resolution.title_changed,
                capture_status=frame.status,
                frame_quality=frame.frame_quality,
                discard_reason=frame.status,
            )
            return None

        # Re-enumerate after capture and before using any title/frame evidence.
        # If the active tab, identity, or candidate cardinality changed during
        # PrintWindow, the frame stays local and is never sent to a provider.
        self._sync_lifecycle()
        self._sync_manual_activity()
        post_capture_validation = self._resolver.validate(identity)
        post_capture_discard = self._window_discard_reason(
            post_capture_validation,
            resolver_generation=capture_resolver_generation,
            window_generation=capture_window_generation,
        )
        if post_capture_discard:
            now = self._clock()
            self._mark_invalid_source(now, post_capture_validation.status)
            self._emit(
                window_status=post_capture_validation.status,
                matched_platform=identity.platform,
                title_match=post_capture_validation.status == "ok",
                title_changed=(
                    resolution.title_changed
                    or post_capture_validation.title_changed
                ),
                capture_status="ok",
                frame_quality=frame.frame_quality,
                validation_stage="post_capture",
                discard_reason=post_capture_discard,
            )
            return None

        now = self._clock()
        self._resolve_content_profile(
            frame,
            identity,
            now=now,
            resolver_generation=capture_resolver_generation,
            window_generation=capture_window_generation,
        )
        # A pause/stop or manual change during the identity call applies
        # within this tick.
        self._sync_lifecycle()
        self._sync_manual_activity()
        return None


def start(
    stop_event: threading.Event,
    pause_event: threading.Event | None = None,
) -> threading.Thread:
    def run() -> None:
        updater = SceneContextUpdater(
            stop_requested=stop_event.is_set,
            pause_requested=(
                pause_event.is_set if pause_event is not None else None
            ),
        )
        log.info(
            "Scene observer started (profile identity=%s)",
            "on" if updater._profile_enabled else "off",
        )
        last_paused = False
        while not stop_event.is_set():
            paused = bool(pause_event and pause_event.is_set())
            if paused != last_paused:
                updater.set_paused(paused)
                last_paused = paused
            if not paused:
                try:
                    updater.tick()
                except Exception as exc:
                    log.warning(
                        "activity shadow tick failed: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
            stop_event.wait(float(cfg.scene.check_interval_sec))
        updater.stop()

    thread = threading.Thread(target=run, name="activity_shadow", daemon=True)
    thread.start()
    return thread
