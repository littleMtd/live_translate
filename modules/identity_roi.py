"""Persistent, normalized channel-identity ROI and exact reviewed-name lookup."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


ROI_STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "identity_rois.json"
_KEY_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_SPACE_RE = re.compile(r"\s+")


def calibration_key(platform: object) -> str:
    """Collapse configured title aliases into the two supported layout owners."""
    value = str(platform or "").strip().casefold()
    if value in {"chzzk", "치지직"}:
        return "chzzk"
    if value == "soop":
        return "soop"
    return ""


@dataclass(frozen=True)
class NormalizedRoi:
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def parse(cls, value: object) -> "NormalizedRoi | None":
        if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
            return None
        values = tuple(value[name] for name in ("x", "y", "width", "height"))
        if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values):
            return None
        roi = cls(*(float(item) for item in values))
        if not all(item == item and abs(item) != float("inf") for item in values):
            return None
        if roi.x < 0 or roi.y < 0 or roi.width <= 0 or roi.height <= 0:
            return None
        if roi.x > 1 or roi.y > 1 or roi.width > 1 or roi.height > 1:
            return None
        if roi.x + roi.width > 1.000001 or roi.y + roi.height > 1.000001:
            return None
        return roi

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class RoiObservation:
    jpeg: bytes
    fingerprint: str
    thumb: bytes


class IdentityRoiStore:
    """Reload a small local state file atomically; malformed state means unavailable."""

    def __init__(self, path: Path = ROI_STATE_PATH):
        self.path = path
        self._signature: tuple[int, int] | None = None
        self._values: dict[str, NormalizedRoi] = {}

    def _reload(self) -> None:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if signature == self._signature:
            return
        self._signature = signature
        self._values = {}
        if signature is None:
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("version") != 1:
            return
        rows = value.get("rois")
        if not isinstance(rows, dict):
            return
        parsed: dict[str, NormalizedRoi] = {}
        for key, raw in rows.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                continue
            roi = NormalizedRoi.parse(raw)
            if roi is not None:
                parsed[key] = roi
        self._values = parsed

    def get(self, key: object) -> NormalizedRoi | None:
        normalized = calibration_key(key)
        if not _KEY_RE.fullmatch(normalized):
            return None
        self._reload()
        return self._values.get(normalized)

    def save(self, key: object, roi: object) -> NormalizedRoi:
        normalized = calibration_key(key)
        parsed = roi if isinstance(roi, NormalizedRoi) else NormalizedRoi.parse(roi)
        if not normalized or parsed is None:
            raise ValueError("identity ROI must be inside a supported player capture")
        self._reload()
        values = dict(self._values)
        values[normalized] = parsed
        payload = {
            "version": 1,
            "rois": {name: value.as_dict() for name, value in sorted(values.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self._signature = None
        self._reload()
        return parsed

    def reset(self, key: object) -> None:
        normalized = calibration_key(key)
        if not normalized:
            raise ValueError("unsupported identity ROI calibration key")
        self._reload()
        values = dict(self._values)
        values.pop(normalized, None)
        payload = {
            "version": 1,
            "rois": {name: value.as_dict() for name, value in sorted(values.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self._signature = None
        self._reload()


def crop_identity_roi(player_jpeg: bytes, roi: NormalizedRoi) -> RoiObservation | None:
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(player_jpeg)).convert("RGB")
        width, height = image.size
    except Exception:
        return None
    left = max(0, min(width - 1, round(roi.x * width)))
    top = max(0, min(height - 1, round(roi.y * height)))
    right = max(left + 1, min(width, round((roi.x + roi.width) * width)))
    bottom = max(top + 1, min(height, round((roi.y + roi.height) * height)))
    if right <= left or bottom <= top:
        return None
    crop = image.crop((left, top, right, bottom))
    buffer = io.BytesIO()
    crop.save(buffer, "JPEG", quality=85)
    jpeg = buffer.getvalue()
    thumb = crop.convert("L").resize((64, 16)).tobytes()
    return RoiObservation(
        jpeg,
        hashlib.blake2s(thumb, digest_size=16).hexdigest(),
        thumb,
    )


IDENTITY_ROI_PROMPT = (
    "Read only the channel owner name visible in this tightly cropped livestream "
    "identity/name block. Return exactly one minified JSON object with one key: "
    '{"identity":"<exact-visible-name-or-empty>"}. Preserve the visible spelling. '
    "If blank, clipped, ambiguous, or unreadable, return an empty identity. Do not "
    "infer a person from appearance, avatar, context, or prior knowledge."
)


def parse_observed_identity(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    try:
        value = json.loads(raw.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(value, dict) or set(value) != {"identity"}:
        return ""
    identity = value.get("identity")
    if not isinstance(identity, str) or len(identity) > 128:
        return ""
    return identity.strip()


def normalize_identity(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _SPACE_RE.sub(" ", normalized)
    if not normalized or len(normalized) > 128 or any(ord(ch) < 32 for ch in normalized):
        return ""
    return normalized.casefold()


def exact_reviewed_member(value: object, registry) -> object | None:
    observed = normalize_identity(value)
    if not observed:
        return None
    matches = {
        marker
        for marker in registry.identity_markers
        if marker.kind == "member_name"
        and any(
            normalize_identity(name) == observed
            for name in (*marker.visible_names, *marker.ocr_aliases)
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def calibration_snapshot() -> dict[str, object]:
    """Capture the exact production player image without persisting it."""
    platform, image = _capture_calibration_image()
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=70)
    return {
        "platform": platform,
        "image_data_url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def _capture_calibration_image():
    """Return platform and the exact in-memory player crop used by production."""
    from PIL import Image
    from modules.scene_context import PrintWindowCaptureBackend, SafeWindowResolver

    resolution = SafeWindowResolver().resolve()
    if resolution.status != "ok" or resolution.identity is None:
        raise RuntimeError(f"supported SOOP/CHZZK player unavailable: {resolution.status}")
    frame = PrintWindowCaptureBackend().capture(resolution.identity)
    if frame.status != "ok" or not frame.content_crop or not frame.jpeg:
        raise RuntimeError(f"supported player capture unavailable: {frame.status}")
    platform = calibration_key(resolution.identity.platform)
    if not platform:
        raise RuntimeError("supported SOOP/CHZZK player unavailable")
    return platform, Image.open(io.BytesIO(frame.jpeg)).convert("RGB")


class IdentityRoiCalibrationWindow:
    """One native calibration UI shared by CLI and optional desktop launchers."""

    _HANDLE_SIZE = 16

    def __init__(self, platform: str, image, *, store: IdentityRoiStore, show_only: bool):
        self.platform = platform
        self.image = image
        self.store = store
        self.show_only = show_only
        self.roi = store.get(platform)
        if self.roi is None and show_only:
            raise RuntimeError(f"no saved identity ROI for {platform.upper()}")
        if self.roi is None:
            self.roi = NormalizedRoi(0.25, 0.25, 0.5, 0.2)
        self.saved = False
        self._drag: tuple[str, float, float, NormalizedRoi] | None = None
        self._root = None
        self._canvas = None
        self._rectangle = None
        self._handle = None

    def run(self) -> bool:
        import tkinter as tk
        from PIL import ImageTk

        root = tk.Tk()
        self._root = root
        root.title(
            f"{self.platform.upper()} saved identity ROI"
            if self.show_only
            else f"{self.platform.upper()} channel identity ROI calibration"
        )
        root.configure(bg="#10151d")
        max_width = max(320, int(root.winfo_screenwidth() * 0.88))
        max_height = max(240, int(root.winfo_screenheight() * 0.78))
        display = self.image.copy()
        display.thumbnail((max_width, max_height))
        self._display_width, self._display_height = display.size
        self._photo = ImageTk.PhotoImage(display)
        canvas = tk.Canvas(
            root,
            width=self._display_width,
            height=self._display_height,
            highlightthickness=0,
            bg="#000000",
        )
        self._canvas = canvas
        canvas.pack(padx=12, pady=(12, 4))
        canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self._rectangle = canvas.create_rectangle(
            *self._pixel_box(), outline="#00f0ff", width=3, fill="", tags=("roi",)
        )
        self._handle = canvas.create_rectangle(
            *self._handle_box(), outline="#001014", fill="#00f0ff", width=2,
            tags=("handle",),
        )
        canvas.create_text(
            self._pixel_box()[0] + 5,
            max(10, self._pixel_box()[1] - 10),
            text="CHANNEL IDENTITY",
            anchor="sw",
            fill="#00f0ff",
            font=("Segoe UI", 10, "bold"),
            tags=("label",),
        )
        if not self.show_only:
            canvas.bind("<ButtonPress-1>", self._on_press)
        controls = tk.Frame(root, bg="#10151d")
        controls.pack(fill="x", padx=12, pady=(4, 12))
        instruction = (
            "Diagnostic only — Esc closes"
            if self.show_only
            else "Drag rectangle to move · cyan corner to resize · Enter saves · Esc cancels"
        )
        tk.Label(controls, text=instruction, bg="#10151d", fg="#d5dbea").pack(side="left")
        if not self.show_only:
            tk.Button(controls, text="Save (Enter)", command=self._save).pack(side="right")
            tk.Button(controls, text="Cancel (Esc)", command=self._cancel).pack(side="right", padx=8)
            root.bind("<Return>", lambda _event: self._save())
        root.bind("<Escape>", lambda _event: self._cancel())
        root.protocol("WM_DELETE_WINDOW", self._cancel)
        root.attributes("-topmost", True)
        root.mainloop()
        return self.saved

    def _pixel_box(self) -> tuple[float, float, float, float]:
        return (
            self.roi.x * self._display_width,
            self.roi.y * self._display_height,
            (self.roi.x + self.roi.width) * self._display_width,
            (self.roi.y + self.roi.height) * self._display_height,
        )

    def _handle_box(self) -> tuple[float, float, float, float]:
        right, bottom = self._pixel_box()[2:]
        half = self._HANDLE_SIZE / 2
        return right - half, bottom - half, right + half, bottom + half

    def _begin(self, event, mode: str) -> None:
        self._drag = (mode, float(event.x), float(event.y), self.roi)
        self._canvas.bind("<B1-Motion>", self._move)
        self._canvas.bind("<ButtonRelease-1>", self._end)

    def _on_press(self, event) -> None:
        x, y = float(event.x), float(event.y)
        handle = self._handle_box()
        rectangle = self._pixel_box()
        if handle[0] <= x <= handle[2] and handle[1] <= y <= handle[3]:
            self._begin(event, "resize")
        elif rectangle[0] <= x <= rectangle[2] and rectangle[1] <= y <= rectangle[3]:
            self._begin(event, "move")

    def _end(self, _event=None) -> None:
        self._drag = None
        self._canvas.unbind("<B1-Motion>")
        self._canvas.unbind("<ButtonRelease-1>")

    def _move(self, event) -> None:
        if self._drag is None:
            return
        mode, start_x, start_y, initial = self._drag
        dx = (float(event.x) - start_x) / self._display_width
        dy = (float(event.y) - start_y) / self._display_height
        if mode == "move":
            self.roi = NormalizedRoi(
                max(0.0, min(1.0 - initial.width, initial.x + dx)),
                max(0.0, min(1.0 - initial.height, initial.y + dy)),
                initial.width,
                initial.height,
            )
        else:
            self.roi = NormalizedRoi(
                initial.x,
                initial.y,
                max(0.01, min(1.0 - initial.x, initial.width + dx)),
                max(0.01, min(1.0 - initial.y, initial.height + dy)),
            )
        self._redraw()

    def _redraw(self) -> None:
        self._canvas.coords(self._rectangle, *self._pixel_box())
        self._canvas.coords(self._handle, *self._handle_box())
        left, top = self._pixel_box()[:2]
        self._canvas.coords("label", left + 5, max(10, top - 10))

    def _save(self) -> None:
        if self.show_only:
            return
        try:
            self.store.save(self.platform, self.roi)
        except Exception as exc:
            from tkinter import messagebox

            messagebox.showerror("Identity ROI save failed", str(exc), parent=self._root)
            return
        self.saved = True
        self._root.destroy()

    def _cancel(self) -> None:
        self.saved = False
        self._root.destroy()


def run_identity_roi_ui(*, show_only: bool = False, store: IdentityRoiStore | None = None) -> bool:
    platform, image = _capture_calibration_image()
    return IdentityRoiCalibrationWindow(
        platform,
        image,
        store=store or IdentityRoiStore(),
        show_only=show_only,
    ).run()


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--calibrate", action="store_true")
    mode.add_argument("--show", action="store_true")
    args = parser.parse_args()
    try:
        if args.calibrate or args.show:
            run_identity_roi_ui(show_only=args.show)
            return 0
        print(json.dumps(calibration_snapshot(), ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        if args.calibrate or args.show:
            print(f"Identity ROI UI failed: {exc}", file=sys.stderr)
            try:
                from tkinter import messagebox

                messagebox.showerror("Identity ROI unavailable", str(exc))
            except Exception:
                pass
        else:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
