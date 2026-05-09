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
        self._label: tk.Label | None = None
        self._toggle_btn: tk.Button | None = None
        self._hide_job = None
        self._drag_x = 0
        self._drag_y = 0
        self._translating = True

    def run(self):
        """Must be called from the main thread."""
        root = tk.Tk()
        self._root = root

        root.overrideredirect(True)       # no title bar
        root.attributes("-topmost", True)
        root.attributes("-alpha", cfg.subtitle.alpha)
        root.configure(bg=cfg.subtitle.bg)

        # Top bar: toggle button (right) + drag handle (left)
        top = tk.Frame(root, bg=cfg.subtitle.bg)
        top.pack(fill="x")

        self._toggle_btn = tk.Button(
            top,
            text="⏸ 翻譯",
            font=("Microsoft JhengHei", 9),
            fg=cfg.subtitle.fg,
            bg="#333333",
            activebackground="#555555",
            activeforeground=cfg.subtitle.fg,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._toggle_translation,
        )
        self._toggle_btn.pack(side="right", padx=6, pady=2)

        label = tk.Label(
            root,
            text="",
            font=cfg.subtitle.font,
            fg=cfg.subtitle.fg,
            bg=cfg.subtitle.bg,
            wraplength=cfg.subtitle.wraplength,
            justify="center",
            padx=cfg.subtitle.padx,
            pady=cfg.subtitle.pady,
        )
        label.pack()
        self._label = label

        # Position bottom-centre of primary screen
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"+{sw // 2 - cfg.subtitle.init_offset_x}+{sh - cfg.subtitle.init_offset_y}")

        # Drag support on all surfaces
        for widget in (root, top, label):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_motion)

        # ESC or double-click to quit; Space to toggle
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
            self._toggle_btn.config(text="⏸ 翻譯")
            log.info("Pipeline resumed")
        else:
            if self._pause:
                self._pause.set()
            self._hide()
            self._drain_all_queues()
            self._toggle_btn.config(text="▶ 翻譯")
            log.info("Pipeline paused")

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

    def _show(self, text: str):
        if self._root is None:
            return
        text = text.strip()
        if not text:
            return
        try:
            wrapped = "\n".join(textwrap.wrap(text, width=cfg.subtitle.max_width_chars)) or text
            self._label.config(text=wrapped)
            log.debug("Subtitle: %s", wrapped)
            if self._hide_job:
                self._root.after_cancel(self._hide_job)
            self._hide_job = self._root.after(cfg.subtitle.idle_hide_ms, self._hide)
        except tk.TclError:
            pass

    def _hide(self):
        try:
            self._label.config(text="")
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
