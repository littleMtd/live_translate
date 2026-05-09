import queue
import threading
import tkinter as tk
import textwrap

from config import cfg
from utils.logger import get_logger

log = get_logger("subtitle_display")


class SubtitleWindow:
    def __init__(self, subtitle_queue: queue.Queue, stop_event: threading.Event,
                 pause_event: threading.Event | None = None,
                 all_queues: list[queue.Queue] | None = None):
        self._queue = subtitle_queue
        self._stop = stop_event
        self._pause = pause_event
        self._all_queues = all_queues or [subtitle_queue]
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._hide_job = None
        self._translating = True
        self._drag_x = 0
        self._drag_y = 0

    def run(self):
        """Must be called from the main thread."""
        root = tk.Tk()
        self._root = root

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", cfg.subtitle.alpha)
        # -transparentcolor makes bg pixels fully transparent + click-through (Windows only)
        root.attributes("-transparentcolor", cfg.subtitle.bg)
        root.configure(bg=cfg.subtitle.bg)

        # Canvas only — no control bar, window is fully transparent
        canvas = tk.Canvas(
            root,
            bg=cfg.subtitle.bg,
            highlightthickness=0,
            bd=0,
            width=1,
            height=1,
        )
        canvas.pack(padx=cfg.subtitle.padx, pady=cfg.subtitle.pady)
        self._canvas = canvas

        # Position bottom-centre of primary screen
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"+{sw // 2 - cfg.subtitle.init_offset_x}+{sh - cfg.subtitle.init_offset_y}")

        # Drag via text pixels — canvas bg is click-through but text pixels are not
        canvas.bind("<ButtonPress-1>", self._on_drag_start)
        canvas.bind("<B1-Motion>", self._on_drag_motion)

        root.bind("<Escape>", lambda e: self._quit())
        root.bind("<Double-Button-1>", lambda e: self._quit())
        root.bind("<space>", lambda e: self._toggle_translation())

        self._poll()
        root.mainloop()

    def _drain_all_queues(self):
        for q in self._all_queues:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _toggle_translation(self):
        self._translating = not self._translating
        if self._translating:
            self._drain_all_queues()
            if self._pause:
                self._pause.clear()
            self._flash("▶")
            log.info("Pipeline resumed")
        else:
            if self._pause:
                self._pause.set()
            self._drain_all_queues()
            self._flash("⏸")
            log.info("Pipeline paused")

    def _flash(self, symbol: str, ms: int = 1500):
        if self._root is None:
            return
        try:
            self._draw_outlined_text(symbol)
            if self._hide_job:
                self._root.after_cancel(self._hide_job)
            self._hide_job = self._root.after(ms, self._hide)
        except tk.TclError:
            pass

    def _poll(self):
        if self._stop.is_set():
            if self._root:
                self._root.destroy()
                self._root = None
            return
        try:
            text = self._queue.get_nowait()
            if self._translating:
                self._show(text)
        except queue.Empty:
            pass
        if self._root:
            self._root.after(cfg.subtitle.poll_interval_ms, self._poll)

    def _draw_outlined_text(self, text: str):
        canvas = self._canvas
        canvas.delete("all")

        ow = cfg.subtitle.outline_width
        pad = ow + cfg.subtitle.padx

        # Measure rendered text size with a temporary probe item
        probe = canvas.create_text(
            pad, pad,
            text=text,
            font=cfg.subtitle.font,
            anchor="nw",
            width=cfg.subtitle.wraplength,
            justify="center",
        )
        bbox = canvas.bbox(probe)
        canvas.delete(probe)
        if not bbox:
            return

        cw = (bbox[2] - bbox[0]) + pad * 2
        ch = (bbox[3] - bbox[1]) + pad * 2
        canvas.config(width=cw, height=ch)
        cx, cy = cw // 2, ch // 2

        # Outline: draw at 8 surrounding positions
        for dx in (-ow, 0, ow):
            for dy in (-ow, 0, ow):
                if dx == 0 and dy == 0:
                    continue
                canvas.create_text(
                    cx + dx, cy + dy,
                    text=text,
                    font=cfg.subtitle.font,
                    fill=cfg.subtitle.outline_color,
                    anchor="center",
                    width=cfg.subtitle.wraplength,
                    justify="center",
                )

        # Main text on top
        canvas.create_text(
            cx, cy,
            text=text,
            font=cfg.subtitle.font,
            fill=cfg.subtitle.fg,
            anchor="center",
            width=cfg.subtitle.wraplength,
            justify="center",
        )

    def _show(self, text: str):
        if self._root is None:
            return
        text = text.strip()
        if not text:
            return
        try:
            wrapped = "\n".join(textwrap.wrap(text, width=cfg.subtitle.max_width_chars)) or text
            self._draw_outlined_text(wrapped)
            log.debug("Subtitle: %s", wrapped)
            if self._hide_job:
                self._root.after_cancel(self._hide_job)
            self._hide_job = self._root.after(cfg.subtitle.idle_hide_ms, self._hide)
        except tk.TclError:
            pass

    def _hide(self):
        try:
            if self._canvas:
                self._canvas.delete("all")
                self._canvas.config(width=1, height=1)
        except tk.TclError:
            pass
        self._hide_job = None

    def _quit(self):
        self._stop.set()
        if self._root:
            self._root.destroy()
            self._root = None

    def _on_drag_start(self, event):
        self._drag_x = event.x_root - self._root.winfo_x()
        self._drag_y = event.y_root - self._root.winfo_y()

    def _on_drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self._root.geometry(f"+{x}+{y}")


def start(subtitle_queue: queue.Queue, stop_event: threading.Event,
          pause_event: threading.Event | None = None,
          all_queues: list[queue.Queue] | None = None):
    """Launch SubtitleWindow on the main thread (blocking). Call from main.py."""
    window = SubtitleWindow(subtitle_queue, stop_event, pause_event, all_queues)
    window.run()


if __name__ == "__main__":
    import time

    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def _feeder():
        samples = ["안녕하세요！歡迎收看今天的直播", "진짜 대박이다 → 真的太狂了", "ㅋㅋㅋ → 哈哈哈"]
        for s in samples:
            time.sleep(2)
            q.put(s)
        time.sleep(4)
        stop.set()

    threading.Thread(target=_feeder, daemon=True).start()
    start(q, stop)
