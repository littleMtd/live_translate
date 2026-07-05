"""Donation OCR translation panel (throwaway value-test prototype).

Watches a fixed screen ROI (the donation feed on the left side of a stream),
OCRs new Korean text with PaddleOCR, translates it through the existing
live_translate translator, and shows source+translation in a small
always-on-top panel. See SCREEN_OCR_MVP_SPEC_20260630.md.

Usage (from repo root, inside live-subtitle-env):
    python donation_ocr/app.py                # standalone
    python donation_ocr/app.py --dry-run      # OCR only, no API calls
    python main.py --donation-ocr             # launch together with the pipeline

Drag the green frame over the donation area (grip at bottom-right resizes).
Close the translation panel window to quit.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "scratch" / "donation_panel"

# ---------------------------------------------------------------- capture

def grab_roi(roi: tuple[int, int, int, int], all_screens: bool):
    from PIL import ImageGrab

    x, y, w, h = roi
    bbox = (x, y, x + w, y + h)
    try:
        return ImageGrab.grab(bbox=bbox, all_screens=all_screens)
    except TypeError:  # older Pillow without all_screens
        return ImageGrab.grab(bbox=bbox)


def frame_changed(prev, img, threshold: float):
    """Cheap change gate: mean abs diff on a small grayscale thumbnail.

    Returns (changed, thumb) so the caller can carry the thumb forward.
    """
    thumb = img.convert("L").resize((64, 64))
    if prev is None:
        return True, thumb
    a = thumb.tobytes()
    b = prev.tobytes()
    diff = sum(abs(pa - pb) for pa, pb in zip(a, b)) / len(a)
    return diff >= threshold, thumb

# ---------------------------------------------------------------- OCR

def normalize_confidence(score) -> float:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0.0
    return value * 100.0 if value <= 1.0 else value


def paddle_rows(raw) -> list[tuple[list, str, float]]:
    """Flatten PaddleOCR output across 2.x/3.x result shapes (from gate1)."""
    rows: list[tuple[list, str, float]] = []
    if raw is None:
        return rows
    if isinstance(raw, dict):
        texts = raw.get("rec_texts") or raw.get("texts") or []
        scores = raw.get("rec_scores") or raw.get("scores") or []
        polys = raw.get("rec_polys") or raw.get("dt_polys") or raw.get("boxes") or []
        for poly, text, score in zip(polys, texts, scores):
            rows.append((poly, str(text or ""), normalize_confidence(score)))
        return rows
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rows.extend(paddle_rows(item))
                continue
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            poly, payload = item[0], item[1]
            if isinstance(payload, (list, tuple)) and len(payload) >= 2:
                rows.append((poly, str(payload[0] or ""), normalize_confidence(payload[1])))
    return rows


def poly_to_xywh(poly) -> tuple[int, int, int, int] | None:
    points = []
    try:
        for point in poly:
            if len(point) >= 2:
                points.append((float(point[0]), float(point[1])))
    except TypeError:
        return None
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return int(min(xs)), int(min(ys)), max(1, int(max(xs) - min(xs))), max(1, int(max(ys) - min(ys)))


def make_paddle(lang: str):
    from paddleocr import PaddleOCR  # heavy import, deferred

    try:
        return PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except TypeError:
        return PaddleOCR(use_angle_cls=True, lang=lang)


def ocr_lines(ocr, image_path: Path, min_conf: float) -> list[tuple[str, tuple[int, int, int, int]]]:
    """OCR a ROI crop; join same-row boxes into lines.

    Returns (text, bbox) per line, bbox in crop coordinates (= relative to
    the ROI origin) so the overlay can draw the translation in place."""
    try:
        raw = ocr.ocr(str(image_path))
    except TypeError:
        raw = ocr.ocr(str(image_path), cls=True)
    boxes = []
    for poly, text, conf in paddle_rows(raw):
        text = text.strip()
        if not text or conf < min_conf:
            continue
        bbox = poly_to_xywh(poly)
        if bbox is None:
            continue
        boxes.append((bbox, text))
    # group by row: boxes whose vertical centers are close belong to one line
    boxes.sort(key=lambda item: item[0][1] + item[0][3] / 2)
    rows: list[list[tuple[tuple, str]]] = []
    for bbox, text in boxes:
        cy = bbox[1] + bbox[3] / 2
        if rows:
            last = rows[-1]
            last_cy = sum(b[1] + b[3] / 2 for b, _ in last) / len(last)
            if abs(cy - last_cy) <= max(12, bbox[3] * 0.7):
                last.append((bbox, text))
                continue
        rows.append([(bbox, text)])
    # within a row, only join boxes that are horizontally adjacent — an
    # unbounded join merged unrelated UI (player clock + donation text,
    # "릴파 2.3 18.4") into one garbage line in the 0705 test sessions
    lines: list[list[tuple[tuple, str]]] = []
    for row in rows:
        row.sort(key=lambda item: item[0][0])
        for bbox, text in row:
            if lines:
                last = lines[-1]
                pb = last[-1][0]
                gap = bbox[0] - (pb[0] + pb[2])
                same_row = abs((bbox[1] + bbox[3] / 2) - (pb[1] + pb[3] / 2)) <= max(12, bbox[3] * 0.7)
                if same_row and gap <= max(28, bbox[3] * 1.5):
                    last.append((bbox, text))
                    continue
            lines.append([(bbox, text)])
    joined = []
    for line in lines:
        text = " ".join(t for _, t in line)
        left = min(b[0] for b, _ in line)
        top = min(b[1] for b, _ in line)
        right = max(b[0] + b[2] for b, _ in line)
        bottom = max(b[1] + b[3] for b, _ in line)
        joined.append((text, (left, top, right - left, bottom - top)))
    return joined


# ---------------------------------------------------------------- filtering / dedup

HANGUL_RE = re.compile(r"[가-힣]")
JUNK_RE = re.compile(r"^[\d\s:./%°C~\-|]+$")  # clocks, dates, meters

# Timecodes / dates / progress bars that OCR picks up from player chrome and
# stream overlays and that pollute both the source and the dedup key
# (observed 0705: "00:00:10", "0:01", "01:44:16/02:47:32", "26-06-30 01-10-03").
_TIMECODE_RE = re.compile(
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*/\s*\d{1,2}:\d{2}(?::\d{2})?)?"
    r"|\d{2,4}[-.]\d{2}[-.]\d{2}(?:[ .]\d{2}[-:]\d{2}[-:]\d{2})?"
)


def strip_timecodes(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _TIMECODE_RE.sub(" ", text)).strip()


def is_candidate(text: str) -> bool:
    if len(text.replace(" ", "")) < 3:
        return False
    if JUNK_RE.match(text):
        return False
    # need at least two Hangul syllables: one lone char is OCR noise
    # ("모We're bie", "[모] Now then")
    if len(HANGUL_RE.findall(text)) < 2:
        return False
    return True


def norm_text(text: str) -> str:
    # drop digits too: OCR jitter renders the same donation's numbers
    # differently frame to frame ("100개" / "!100개" / "1007100")
    return re.sub(r"[\s\W_\d]+", "", text, flags=re.UNICODE).lower()


class RecencyCache:
    """Skip texts we already handled recently (donation list lingers on screen)."""

    def __init__(self, ttl_s: float, similarity: float):
        self.ttl_s = ttl_s
        self.similarity = similarity
        self.entries: list[tuple[str, float]] = []

    @staticmethod
    def _same(a: str, b: str, threshold: float) -> bool:
        if a == b:
            return True
        if SequenceMatcher(None, a, b).ratio() >= threshold:
            return True
        # containment: OCR truncation yields fragments of an already-seen
        # line ("백연호님별풍선개감" vs "호님별풍선개감") that plain ratio
        # misses once lengths diverge
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if len(short) >= 6:
            match = SequenceMatcher(None, short, long_).find_longest_match(
                0, len(short), 0, len(long_))
            if match.size / len(short) >= 0.8:
                return True
        return False

    def seen(self, norm: str, now: float) -> bool:
        self.entries = [(n, t) for n, t in self.entries if now - t <= self.ttl_s]
        return any(self._same(cached, norm, self.similarity) for cached, _ in self.entries)

    def add(self, norm: str, now: float) -> None:
        self.entries.append((norm, now))


# Model meta-responses that must never be shown as a translation
# (observed 0705: qwen answering "（輸出零個字元）" to OCR garbage).
_META_TARGET_RE = re.compile(r"^[（(][^）)]{0,30}[）)]$")


def clean_target(target: str) -> str:
    return "" if _META_TARGET_RE.match(target.strip()) else target


# ---------------------------------------------------------------- worker

def worker_loop(args, ui_queue: queue.Queue, stop: threading.Event, shared: dict) -> None:
    ocr = make_paddle(args.paddle_lang)
    ui_queue.put(("status", "PaddleOCR 已載入"))

    translator = None
    if not args.dry_run:
        sys.path.insert(0, str(REPO_ROOT))
        from config import cfg  # noqa: deferred so --help/--dry-run stay light

        if args.profile:
            try:
                cfg.translation.streamer_profile = args.profile
            except Exception:
                object.__setattr__(cfg.translation, "streamer_profile", args.profile)
            try:
                cfg.translation.use_profile = True
            except Exception:
                object.__setattr__(cfg.translation, "use_profile", True)
        from modules import translator as translator_mod

        # 本面板是獨立行程:主管線同時在跑時(說話和斗內本來就同時發生),
        # 兩行程共寫 SQLite 會頻繁撞鎖。斗內文字幾乎一次性,DB cache 無
        # 價值;歷史已有 session_*.jsonl。故此行程完全不碰 DB/歷史檔——
        # 在建 Translator 前把 db factory 與 history writer 換成 no-op,
        # in-memory cache 照常運作。
        class _NullDB:
            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        _null_db = _NullDB()
        translator_mod._get_db = lambda: _null_db
        translator_mod._write_history = lambda *args, **kwargs: None
        translator = translator_mod.Translator()
        ui_queue.put(("status", f"translator 已載入 (profile={cfg.active_streamer_profile}, DB-less)"))

    cache = RecencyCache(ttl_s=args.dedup_ttl, similarity=args.dedup_similarity)
    tmp_png = OUT_DIR / "_roi_live.png"
    log_path = OUT_DIR / f"session_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    prev_thumb = None
    batch = 0  # 一次擷取裡的新行算同一批;新批出現時 UI 整批取代舊標籤
    uid = 0

    while not stop.is_set():
        started = time.monotonic()
        try:
            roi = shared.get("roi")
            if roi is None or roi[2] < 40 or roi[3] < 30:
                stop.wait(0.5)
                continue
            img = grab_roi(roi, args.all_screens)
            changed, prev_thumb = frame_changed(prev_thumb, img, args.change_threshold)
            if changed:
                img.save(tmp_png)
                now = time.monotonic()
                fresh: list[tuple[int, str, tuple]] = []
                for line, bbox in ocr_lines(ocr, tmp_png, args.min_conf):
                    line = strip_timecodes(line)
                    if not is_candidate(line):
                        continue
                    norm = norm_text(line)
                    if cache.seen(norm, now):
                        continue
                    cache.add(norm, now)
                    uid += 1
                    fresh.append((uid, line, bbox))
                if fresh:
                    batch += 1
                    # 先全部立刻上屏(原文),翻譯好再逐一原地換字
                    for item_id, line, bbox in fresh:
                        ui_queue.put(("show", batch, item_id, line, bbox,
                                      translator is None))
                    for item_id, line, _bbox in fresh:
                        target = ""
                        if translator is not None:
                            try:
                                target = clean_target(translator.translate(line) or "")
                            except Exception as exc:  # keep panel alive on API hiccups
                                target = f"[翻譯失敗: {type(exc).__name__}]"
                            ui_queue.put(("update", item_id, line, target))
                        with open(log_path, "a", encoding="utf-8") as handle:
                            handle.write(json.dumps(
                                {"ts": datetime.now().isoformat(timespec="seconds"),
                                 "source": line, "target": target},
                                ensure_ascii=False) + "\n")
        except Exception as exc:
            ui_queue.put(("status", f"錯誤: {type(exc).__name__}: {exc}"))
        # pace the loop regardless of how long OCR took
        remaining = args.interval - (time.monotonic() - started)
        if remaining > 0:
            stop.wait(remaining)


# ---------------------------------------------------------------- UI

ROI_STATE = OUT_DIR / "roi_window.json"
TRANSPARENT = "#010203"  # unlikely color; becomes see-through on Windows
BORDER_PX = 6            # also the grab target for dragging, keep it chunky
GRIP_PX = 16             # bottom-right resize handle


def load_selector_geometry(args) -> str:
    try:
        saved = json.loads(ROI_STATE.read_text(encoding="utf-8"))
        return saved["geometry"]
    except Exception:
        x, y, w, h = args.roi
        return f"{w}x{h}+{x}+{y}"


def make_selector(root, args):
    """Borderless hollow frame window: drag the green border to move,
    drag the bottom-right grip to resize. Interior is click-through."""
    import tkinter as tk

    sel = tk.Toplevel(root)
    sel.overrideredirect(True)  # no title bar - just the frame
    sel.geometry(load_selector_geometry(args))
    sel.attributes("-topmost", True)
    sel.configure(bg=TRANSPARENT)
    try:
        sel.attributes("-transparentcolor", TRANSPARENT)
    except tk.TclError:  # non-Windows fallback: semi-transparent instead of hollow
        sel.attributes("-alpha", 0.25)
    inner = tk.Frame(sel, bg=TRANSPARENT, cursor="fleur",
                     highlightbackground="#00d26a", highlightcolor="#00d26a",
                     highlightthickness=BORDER_PX)
    inner.pack(fill="both", expand=True)
    grip = tk.Frame(sel, bg="#00d26a", width=GRIP_PX, height=GRIP_PX,
                    cursor="bottom_right_corner")
    grip.place(relx=1.0, rely=1.0, anchor="se")

    drag: dict = {}

    def press(event, resize: bool) -> None:
        drag.update(x=event.x_root, y=event.y_root,
                    gx=sel.winfo_x(), gy=sel.winfo_y(),
                    gw=sel.winfo_width(), gh=sel.winfo_height(),
                    resize=resize)

    def motion(event) -> None:
        if not drag:
            return
        dx = event.x_root - drag["x"]
        dy = event.y_root - drag["y"]
        if drag["resize"]:
            w = max(120, drag["gw"] + dx)
            h = max(80, drag["gh"] + dy)
            sel.geometry(f"{w}x{h}")
        else:
            sel.geometry(f"+{drag['gx'] + dx}+{drag['gy'] + dy}")

    inner.bind("<Button-1>", lambda e: press(e, resize=False))
    inner.bind("<B1-Motion>", motion)
    grip.bind("<Button-1>", lambda e: press(e, resize=True))
    grip.bind("<B1-Motion>", motion)
    return sel


def make_overlay(root):
    """Click-through transparent canvas covering the ROI: translations are
    drawn in place, on top of the OCR'd line (Lens-style)."""
    import tkinter as tk

    ov = tk.Toplevel(root)
    ov.overrideredirect(True)
    ov.attributes("-topmost", True)
    ov.configure(bg=TRANSPARENT)
    try:
        ov.attributes("-transparentcolor", TRANSPARENT)
    except tk.TclError:
        ov.attributes("-alpha", 0.7)
    canvas = tk.Canvas(ov, bg=TRANSPARENT, highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    ov.update_idletasks()
    if sys.platform == "win32":  # visible pixels must not eat clicks either
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(ov.winfo_id())
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        except Exception:
            pass
    return ov, canvas


def run_panel(args, ui_queue: queue.Queue, stop: threading.Event, shared: dict) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("斗內翻譯")
    root.geometry(args.geometry)
    root.attributes("-topmost", True)
    root.configure(bg="#161616")
    try:
        root.attributes("-alpha", args.alpha)
    except tk.TclError:
        pass

    status = tk.Label(root, text="啟動中…", anchor="w", bg="#161616", fg="#7a7a7a",
                      font=("Microsoft JhengHei", 9))
    status.pack(fill="x", padx=8, pady=(6, 0))
    text = tk.Text(root, bg="#161616", fg="#e8e8e8", relief="flat", wrap="word",
                   font=("Microsoft JhengHei", 12), state="disabled",
                   padx=10, pady=8, spacing1=2, spacing3=10)
    text.pack(fill="both", expand=True)
    text.tag_configure("src", foreground="#8f8f8f", font=("Malgun Gothic", 10))
    text.tag_configure("tgt", foreground="#ffffff", font=("Microsoft JhengHei", 13, "bold"))
    text.tag_configure("time", foreground="#5c5c5c", font=("Consolas", 8))

    selector = make_selector(root, args)
    overlay = canvas = None
    if not args.no_overlay:
        overlay, canvas = make_overlay(root)
    labels: dict[int, dict] = {}      # uid -> {rect, text, born}
    view = {"batch": None}            # 目前上屏的批次;新批出現→整批取代

    def poll_roi() -> None:
        # selector 的內容區(去掉邊框)就是 OCR 區域;worker 每輪讀最新值
        try:
            x = selector.winfo_rootx() + BORDER_PX
            y = selector.winfo_rooty() + BORDER_PX
            w = selector.winfo_width() - BORDER_PX * 2
            h = selector.winfo_height() - BORDER_PX * 2
            shared["roi"] = (x, y, w, h)
            if overlay is not None:  # 覆蓋窗貼齊 ROI 內容區
                overlay.geometry(f"{max(1, w)}x{max(1, h)}+{x}+{y}")
        except tk.TclError:
            pass
        root.after(300, poll_roi)

    def clear_labels() -> None:
        for item in labels.values():
            canvas.delete(item["rect"])
            canvas.delete(item["text"])
        labels.clear()

    def draw_in_place(uid: int, batch: int, source: str, bbox) -> None:
        if canvas is None:
            return
        if view["batch"] != batch:  # 斗內一次只有一則:新批直接取代舊標籤
            clear_labels()
            view["batch"] = batch
        x, y, w, _h = bbox
        text_id = canvas.create_text(
            max(0, x), max(0, y), anchor="nw", text=source,
            font=("Microsoft JhengHei", args.overlay_font, "bold"),
            fill="#ffffff", width=max(w, 240))
        tb = canvas.bbox(text_id)
        rect_id = canvas.create_rectangle(
            tb[0] - 4, tb[1] - 2, tb[2] + 4, tb[3] + 2,
            fill="#101010", outline="#00d26a")
        canvas.tag_lower(rect_id, text_id)
        labels[uid] = {"rect": rect_id, "text": text_id, "born": time.monotonic()}

    def update_label(uid: int, target: str) -> None:
        item = labels.get(uid)  # 已被新批取代就默默略過
        if item is None or canvas is None:
            return
        canvas.itemconfigure(item["text"], text=target)
        tb = canvas.bbox(item["text"])
        canvas.coords(item["rect"], tb[0] - 4, tb[1] - 2, tb[2] + 4, tb[3] + 2)
        item["born"] = time.monotonic()  # 譯文剛換上,重算停留時間

    def expire_overlay() -> None:
        if canvas is not None:
            now = time.monotonic()
            for uid_ in [u for u, it in labels.items()
                         if now - it["born"] > args.overlay_ttl]:
                item = labels.pop(uid_)
                canvas.delete(item["rect"])
                canvas.delete(item["text"])
        root.after(500, expire_overlay)

    def panel_insert(source: str, target: str) -> None:
        text.configure(state="normal")
        stamp = datetime.now().strftime("%H:%M:%S")
        text.insert("1.0", f"{stamp}\n{source}\n{target or '(無譯文)'}\n\n", ())
        text.tag_add("time", "1.0", "1.end")
        text.tag_add("src", "2.0", "2.end")
        text.tag_add("tgt", "3.0", "3.end")
        lines = int(text.index("end-1c").split(".")[0])
        max_lines = args.max_rows * 4
        if lines > max_lines:
            text.delete(f"{max_lines}.0", "end")
        text.configure(state="disabled")

    def pump() -> None:
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg[0] == "status":
                    status.configure(text=msg[1])
                elif msg[0] == "show":
                    _, batch, uid_, source, bbox, final = msg
                    draw_in_place(uid_, batch, source, bbox)
                    if final:  # dry-run:沒有後續 update,直接進歷史
                        panel_insert(source, "")
                elif msg[0] == "update":
                    _, uid_, source, target = msg
                    update_label(uid_, target or "(無譯文)")
                    panel_insert(source, target)
        except queue.Empty:
            pass
        root.after(200, pump)

    def on_close() -> None:
        try:
            ROI_STATE.write_text(
                json.dumps({"geometry": selector.geometry()}), encoding="utf-8")
        except Exception:
            pass
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    selector.protocol("WM_DELETE_WINDOW", on_close)
    root.after(200, pump)
    root.after(100, poll_roi)
    root.after(500, expire_overlay)
    root.mainloop()


# ---------------------------------------------------------------- main

def parse_roi(raw: str) -> list[int]:
    parts = [int(p) for p in raw.replace(" ", "").split(",")]
    if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
        raise argparse.ArgumentTypeError("ROI 格式: x,y,w,h (w/h > 0)")
    return parts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roi", type=parse_roi, default=[0, 680, 640, 380],
                        help="選區框的初始 x,y,w,h(僅第一次;之後記住上次位置)")
    parser.add_argument("--interval", type=float, default=2.5, help="擷取間隔秒")
    parser.add_argument("--min-conf", type=float, default=70.0)
    parser.add_argument("--profile", default="isegye_lilpa",
                        help="streamer profile;空字串=沿用 config 現值")
    parser.add_argument("--dry-run", action="store_true",
                        help="只 OCR 不翻譯(不 import translator、不打 API)")
    parser.add_argument("--paddle-lang", default="korean")
    parser.add_argument("--change-threshold", type=float, default=1.5,
                        help="幀差門檻(64x64 灰階平均絕對差)")
    parser.add_argument("--dedup-ttl", type=float, default=180.0)
    parser.add_argument("--dedup-similarity", type=float, default=0.85)
    parser.add_argument("--max-rows", type=int, default=8, help="面板保留最近 N 則")
    parser.add_argument("--geometry", default="400x560+660+300", help="tk geometry")
    parser.add_argument("--alpha", type=float, default=0.94)
    parser.add_argument("--all-screens", action="store_true", help="多螢幕時擷取全部虛擬桌面")
    parser.add_argument("--no-overlay", action="store_true",
                        help="關閉就地覆蓋,只用面板顯示")
    parser.add_argument("--overlay-ttl", type=float, default=8.0,
                        help="就地譯文停留秒數(清單捲動後自動過期)")
    parser.add_argument("--overlay-font", type=int, default=12, help="就地譯文字級")
    args = parser.parse_args(argv)

    # make ImageGrab coordinates physical pixels under Windows DPI scaling
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("綠色空心框 = OCR 區域:抓綠邊拖動、抓右下角小方塊縮放,即時生效。")
    print("框的位置會記住,下次啟動自動還原。關翻譯面板即結束。")
    if args.dry_run:
        print("[dry-run] 只 OCR 不翻譯。")

    shared: dict = {"roi": None}
    ui_queue: queue.Queue = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(
        target=worker_loop, args=(args, ui_queue, stop, shared), daemon=True)
    thread.start()
    run_panel(args, ui_queue, stop, shared)
    stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
