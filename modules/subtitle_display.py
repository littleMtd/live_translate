import queue
import threading
import time
import tkinter as tk
import textwrap
from datetime import datetime

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from modules.provisional_subtitles import SubtitlePayload
from utils.runtime_events import runtime_events
from modules.live_status import live_status
from modules.profile_context import profile_state
from modules import bookmarks
from modules.global_hotkeys import GlobalHotkeys

log = get_logger("subtitle_display")


_PREVIOUS_ROW_FALLBACK_CHARS = 24  # fits 700 px at 15 pt even at 125 % display scaling


def _one_row(text: str) -> str:
    """The previous subtitle squeezed into one row: its last part, with … when cut.

    Measured in pixels against the canvas wrap width, so it never wraps to a second row;
    without a Tk font (tests, early startup) a conservative character count is used.
    """
    flat = text.replace("\n", "")
    try:
        from tkinter import font as tkfont

        measure = tkfont.Font(font=cfg.subtitle.previous_font).measure
        fits = lambda s: measure(s) <= cfg.subtitle.wraplength  # noqa: E731
    except (tk.TclError, RuntimeError):
        fits = lambda s: len(s) <= _PREVIOUS_ROW_FALLBACK_CHARS  # noqa: E731
    if fits(flat):
        return flat
    keep = len(flat) - 1
    while keep > 0 and not fits("…" + flat[-keep:]):
        keep -= 1
    return "…" + flat[-keep:] if keep > 0 else "…"


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
        self._pending_text: SubtitlePayload | None = None
        self._current_subtitle_id = ""
        self._current_subtitle_revision = -1
        # Session ledger: retaining finalized IDs is what makes the invariant
        # hold even for an arbitrarily delayed producer update.
        self._accepted_revisions: dict[str, tuple[int, bool]] = {}
        self._show_time: float = 0.0
        self._show_min_ms: int = 0
        # Status indicator / history overlay state.
        self._visible_text = ""          # wrapped subtitle currently on screen ("" = hidden)
        self._current_line = ""          # wrapped text of the latest real subtitle (not a ⏸/▶ flash)
        self._previous_line = ""         # the subtitle before it, drawn small above it
        self._status_labels: list[str] = []
        self._status_checked_at = 0.0
        self._history_until = 0.0        # monotonic deadline while the history view is open
        self._history_home: tuple[int, int] | None = None  # window position to restore on close
        self._opened_at = time.monotonic()
        self._notice = ""                # short status-line notice ("★ 已標記", Esc prompt)
        self._notice_until = 0.0
        self._quit_armed_until = 0.0     # Esc must be pressed twice within notice_ms to close
        # (bookmark, after() job, formatted text once a write has been attempted)
        self._pending_marks: list[tuple[bookmarks.Bookmark, object, str | None]] = []
        self._hotkeys: GlobalHotkeys | None = None

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
        # Right-click the text: recent subtitles with their Korean source.
        canvas.bind("<ButtonPress-3>", lambda e: self._toggle_history())

        # Middle-click the text (or press M): bookmark this moment.
        canvas.bind("<ButtonPress-2>", lambda e: self._mark())
        root.bind("<m>", lambda e: self._mark())
        root.bind("<M>", lambda e: self._mark())

        # Esc twice closes the window; a single stray key or a double-click while
        # dragging no longer ends the stream.
        root.bind("<Escape>", lambda e: self._request_quit())
        root.bind("<space>", lambda e: self._toggle_translation())
        self._opened_at = time.monotonic()

        # The bindings above need keyboard focus; these work while the player has it.
        if cfg.subtitle.global_hotkeys_enabled:
            self._hotkeys = GlobalHotkeys({
                "bookmark": cfg.subtitle.hotkey_bookmark,
                "pause": cfg.subtitle.hotkey_pause,
                "history": cfg.subtitle.hotkey_history,
            })
            self._hotkeys.start()

        self._poll()
        try:
            root.mainloop()
        finally:
            if self._hotkeys is not None:
                self._hotkeys.stop()

    def _run_hotkey_actions(self) -> None:
        if self._hotkeys is None:
            return
        handlers = {"bookmark": self._mark, "pause": self._toggle_translation, "history": self._toggle_history}
        unavailable = self._hotkeys.unavailable()
        if unavailable:
            try:
                self._show_notice("快捷鍵無法使用：" + "、".join(unavailable))
            except Exception:
                log.exception("Could not show the unavailable-hotkey notice")
        for action in self._hotkeys.poll():
            try:
                handlers[action]()
            except Exception:  # an error here must not stop _poll from rescheduling itself
                log.exception("Global hotkey action %s failed", action)

    def _drain_all_queues(self):
        for q in self._all_queues:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _toggle_translation(self):
        self._close_history()
        self._translating = not self._translating
        self._current_line = ""
        self._previous_line = ""
        # Queue draining does not remove a line already held for the
        # reading-speed guard. It is stale after any pause-state change.
        self._pending_text = None
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
            self._flash("⏸", ms=None)
            log.info("Pipeline paused")

    def _withdraw(self, subtitle_id: str) -> None:
        if not subtitle_id:
            return
        if self._pending_text is not None and self._pending_text.subtitle_id == subtitle_id:
            self._pending_text = None
        elif subtitle_id == self._current_subtitle_id:
            # Drop the withdrawn line; the last line that was really shown takes its place.
            self._current_line, self._previous_line = self._previous_line, ""
            self._visible_text = self._current_line
            self._current_subtitle_id = ""
            self._show_min_ms = 0  # the withdrawn line's reading time no longer holds the next one
            self._redraw()
        else:
            return
        metrics.increment("subtitle.preview_withdrawn")
        runtime_events.emit("provisional_translation", action="display_withdrawn", provisional_id=subtitle_id)

    def _cancel_hide(self):
        if self._root is None or self._hide_job is None:
            self._hide_job = None
            return
        try:
            self._root.after_cancel(self._hide_job)
        except tk.TclError:
            pass
        self._hide_job = None

    def _accept_revision(self, payload: SubtitlePayload) -> bool:
        """Enforce one monotonic revision order for queued, pending and shown text."""
        if not payload.subtitle_id:
            return True
        version = (payload.revision, payload.phase == "final")
        previous = self._accepted_revisions.get(payload.subtitle_id)
        if previous is not None and version < previous:
            metrics.increment("subtitle.revision_regression_dropped")
            runtime_events.emit(
                "provisional_translation",
                action="display_dropped",
                provisional_id=payload.subtitle_id,
                reason="revision_regression",
                revision=payload.revision,
            )
            return False
        self._accepted_revisions[payload.subtitle_id] = version
        return True

    def _flash(self, symbol: str, ms: int | None = 1500):
        if self._root is None:
            return
        try:
            # Track it as the visible text so a status redraw keeps the symbol.
            self._visible_text = symbol
            self._draw_outlined_text(symbol)
            self._cancel_hide()
            if ms is not None:
                self._hide_job = self._root.after(ms, self._hide)
        except tk.TclError:
            pass

    def _poll(self):
        if self._stop.is_set():
            self._flush_marks()
            if self._root:
                self._root.destroy()
                self._root = None
            return

        self._run_hotkey_actions()
        now = time.monotonic()
        if self._history_until:
            if now < self._history_until:
                # History is on screen: leave subtitles queued so their display
                # timing and telemetry start only once they are actually drawn.
                self._refresh_overlay(now)
                if self._root:
                    self._root.after(cfg.subtitle.poll_interval_ms, self._poll)
                return
            self._close_history()

        # Drain queue — always keep the latest subtitle
        try:
            while True:
                candidate = self._queue.get_nowait()
                if self._translating:
                    if isinstance(candidate, str):
                        candidate = SubtitlePayload(
                            text=candidate,
                            subtitle_id="",
                            phase="final",
                        )
                    if not isinstance(candidate, SubtitlePayload):
                        continue
                    if not self._accept_revision(candidate):
                        continue
                    if not candidate.text.strip():
                        # The translator withdrew this subtitle's preview: its final
                        # will not be shown, so the unchecked preview must go too.
                        self._withdraw(candidate.subtitle_id)
                        continue
                    if (
                        candidate.subtitle_id
                        and candidate.subtitle_id == self._current_subtitle_id
                    ):
                        self._show(candidate.text, new_line=False)
                        self._current_subtitle_revision = candidate.revision
                        self._pending_text = None
                        metrics.increment("subtitle.revision_replaced")
                        runtime_events.emit(
                            "provisional_translation",
                            action="final_revision_displayed",
                            provisional_id=candidate.subtitle_id,
                            revision=candidate.revision,
                        )
                        continue
                    if self._pending_text is not None and self._pending_text != candidate:
                        # L15: mirror of translator-side stale_skipped — a
                        # pending subtitle was replaced before it was shown.
                        metrics.increment("subtitle.pending_overwritten")
                        if self._pending_text.phase == "provisional":
                            runtime_events.emit(
                                "provisional_translation",
                                action="display_dropped",
                                provisional_id=self._pending_text.subtitle_id,
                                reason="pending_overwritten",
                            )
                    self._pending_text = candidate
        except queue.Empty:
            pass

        # Display pending subtitle only after minimum hold time has elapsed
        if self._pending_text and self._translating:
            elapsed_ms = (time.monotonic() - self._show_time) * 1000
            if elapsed_ms >= self._show_min_ms:
                payload = self._pending_text
                self._show(payload.text)
                self._current_subtitle_id = payload.subtitle_id
                self._current_subtitle_revision = payload.revision
                self._pending_text = None
                if payload.phase == "provisional":
                    runtime_events.emit(
                        "provisional_translation",
                        action="displayed",
                        provisional_id=payload.subtitle_id,
                        revision=payload.revision,
                    )

        self._refresh_overlay(now)

        if self._root:
            self._root.after(cfg.subtitle.poll_interval_ms, self._poll)

    # --- status indicator and history -------------------------------------------------

    def _current_status_labels(self) -> list[str]:
        labels: list[str] = []
        if cfg.subtitle.status_indicator_enabled and self._translating:
            snapshot = profile_state.current()
            unresolved = snapshot.mode == "auto" and not snapshot.effective_profile_id
            labels = live_status.flags(profile_unresolved=unresolved)
        if self._active_notice():
            labels = [*labels, self._notice]
        return labels

    def _show_notice(self, text: str) -> None:
        self._notice = text
        self._notice_until = time.monotonic() + cfg.subtitle.notice_ms / 1000
        self._status_checked_at = 0.0  # redraw now, not at the next once-a-second check
        self._refresh_overlay(time.monotonic())

    def _active_notice(self) -> str:
        return self._notice if self._notice and time.monotonic() < self._notice_until else ""

    # --- bookmarks and closing ---------------------------------------------------------

    def _mark(self) -> None:
        now = time.monotonic()
        # A second press right after the first (click plus key, or a bounce) marks the same moment.
        if self._pending_marks and now - self._pending_marks[-1][0].marked_at < 2.0:
            self._show_notice("★ 已標記")
            return
        before = cfg.subtitle.bookmark_before_lines
        mark = bookmarks.Bookmark(
            marked_at=now,
            wall_time=datetime.now(),
            elapsed_sec=now - self._opened_at,
            profile_id=profile_state.current().effective_profile_id or "",
            before=tuple(live_status.recent_subtitles(before)) if before > 0 else (),
            after_sec=cfg.subtitle.bookmark_after_ms / 1000,
        )
        job = None
        if self._root is not None:
            try:
                job = self._root.after(cfg.subtitle.bookmark_after_ms, lambda: self._write_mark(mark))
            except tk.TclError:
                job = None
        self._pending_marks.append((mark, job, None))
        if job is None:
            self._write_mark(mark)
        self._show_notice("★ 已標記")
        log.info("Bookmark marked")

    def _render_mark(self, mark: bookmarks.Bookmark) -> str:
        return bookmarks.format_bookmark(mark, live_status.recent_subtitles(100))

    def _write_mark(self, mark: bookmarks.Bookmark) -> bool:
        """Write one bookmark; on failure its text stays pending so closing retries it once."""
        text = None
        try:
            text = self._render_mark(mark)
            path = bookmarks.write_text(mark, text)
        except Exception:  # never let a file problem break the Tk loop or the close path
            log.exception("Failed to save bookmark")
            self._pending_marks = [(m, None, text) if m is mark else (m, j, t)
                                   for m, j, t in self._pending_marks]
            self._show_notice("標記儲存失敗")
            return False
        self._pending_marks = [(m, j, t) for m, j, t in self._pending_marks if m is not mark]
        log.info("Bookmark saved to %s", path.name)
        return True

    def _flush_marks(self) -> None:
        """Write every pending bookmark now (the window is closing); give up on failures."""
        pending, self._pending_marks = list(self._pending_marks), []
        for mark, job, text in pending:
            if job is not None and self._root is not None:
                try:
                    self._root.after_cancel(job)
                except tk.TclError:
                    pass
            try:
                bookmarks.write_text(mark, text if text is not None else self._render_mark(mark))
            except Exception:
                log.exception("Failed to save bookmark while closing")

    def _request_quit(self) -> None:
        now = time.monotonic()
        if now < self._quit_armed_until:
            self._quit()
            return
        self._quit_armed_until = now + cfg.subtitle.notice_ms / 1000
        self._show_notice("再按一次 Esc 關閉")

    def _refresh_overlay(self, now: float) -> None:
        """Once a second: redraw when the status labels change."""
        if now - self._status_checked_at < 1.0:
            return
        self._status_checked_at = now
        labels = self._current_status_labels()
        if labels != self._status_labels:
            self._status_labels = labels
            self._redraw()  # the history view shows notices too

    def _redraw(self) -> None:
        if self._history_until:
            self._draw_history()
        elif self._visible_text or self._status_labels:
            self._draw_outlined_text(self._visible_text)
        else:
            self._clear_canvas()

    def _toggle_history(self) -> None:
        if self._history_until:
            self._close_history()
            return
        if not self._translating:
            return  # paused: the pause symbol owns the window
        self._history_until = time.monotonic() + cfg.subtitle.history_display_ms / 1000
        self._history_home = self._window_position()
        self._redraw()
        self._keep_on_screen()

    def _close_history(self) -> None:
        if not self._history_until:
            return
        self._history_until = 0.0
        self._redraw()
        if self._history_home is not None:
            self._move_window(*self._history_home)
            self._history_home = None

    def _window_position(self) -> tuple[int, int] | None:
        try:
            return int(self._root.winfo_x()), int(self._root.winfo_y())
        except (AttributeError, tk.TclError):
            return None

    def _move_window(self, x: int, y: int) -> None:
        try:
            self._root.geometry(f"+{x}+{y}")
        except (AttributeError, tk.TclError):
            pass

    def _keep_on_screen(self) -> None:
        """Lift the window while a tall history view would run past the screen bottom."""
        home = self._history_home
        if home is None or self._canvas is None:
            return
        try:
            self._root.update_idletasks()
            height = int(self._root.winfo_reqheight())
            screen = int(self._root.winfo_screenheight())
        except (AttributeError, tk.TclError):
            return
        limit = screen - height - 48  # leave room for the taskbar
        if home[1] > limit:
            self._move_window(home[0], max(0, limit))

    def _draw_history(self) -> None:
        records = live_status.recent_subtitles(cfg.subtitle.history_lines)
        lines: list[tuple[str, tuple, str]] = []
        if self._active_notice():
            lines.append(("● " + self._notice, cfg.subtitle.status_font, cfg.subtitle.status_color))
        if not records:
            lines.append(("（還沒有字幕）", cfg.subtitle.history_font, cfg.subtitle.fg))
        for record in records:
            if record.source:
                lines.append((record.source, cfg.subtitle.history_source_font, cfg.subtitle.history_source_color))
            lines.append((record.target, cfg.subtitle.history_font, cfg.subtitle.fg))
        self._draw_lines(lines, justify="left")

    def _clear_canvas(self) -> None:
        try:
            if self._canvas:
                self._canvas.delete("all")
                self._canvas.config(width=1, height=1)
        except tk.TclError:
            pass

    def _draw_outlined_text(self, text: str):
        """The subtitle, with the status line above it while something needs attention."""
        lines: list[tuple[str, tuple, str]] = []
        if self._status_labels:
            lines.append(("● " + " · ".join(self._status_labels), cfg.subtitle.status_font, cfg.subtitle.status_color))
        if text and text == self._current_line and cfg.subtitle.show_previous_line:
            # Always one row (a blank one when there is no previous line), so the current
            # subtitle stays at the same height on screen instead of jumping down.
            lines.append((_one_row(self._previous_line) or " ", cfg.subtitle.previous_font, cfg.subtitle.previous_color))
        if text:
            lines.append((text, cfg.subtitle.font, cfg.subtitle.fg))
        self._draw_lines(lines, justify="center")

    def _draw_lines(self, lines: list[tuple[str, tuple, str]], *, justify: str) -> None:
        """Stack outlined text lines (text, font, colour) and size the canvas to fit."""
        canvas = self._canvas
        if canvas is None:
            return
        canvas.delete("all")
        lines = [line for line in lines if line[0]]
        if not lines:
            canvas.config(width=1, height=1)
            return

        ow = cfg.subtitle.outline_width
        pad = ow + cfg.subtitle.padx
        gap = 4

        # Measure each line with a temporary probe item
        sizes = []
        for text, font, _colour in lines:
            probe = canvas.create_text(
                0, 0, text=text, font=font, anchor="nw",
                width=cfg.subtitle.wraplength, justify=justify,
            )
            bbox = canvas.bbox(probe)
            canvas.delete(probe)
            sizes.append((bbox[2] - bbox[0], bbox[3] - bbox[1]) if bbox else (0, 0))

        cw = max(w for w, _h in sizes) + pad * 2
        ch = sum(h for _w, h in sizes) + gap * (len(sizes) - 1) + pad * 2
        canvas.config(width=cw, height=ch)

        anchor = "n" if justify == "center" else "nw"
        x = cw // 2 if justify == "center" else pad
        y = pad
        for (text, font, colour), (_w, h) in zip(lines, sizes):
            # Outline: draw at 8 surrounding positions, then the text on top
            for dx in (-ow, 0, ow):
                for dy in (-ow, 0, ow):
                    if dx == 0 and dy == 0:
                        continue
                    canvas.create_text(
                        x + dx, y + dy, text=text, font=font,
                        fill=cfg.subtitle.outline_color, anchor=anchor,
                        width=cfg.subtitle.wraplength, justify=justify,
                    )
            canvas.create_text(
                x, y, text=text, font=font, fill=colour, anchor=anchor,
                width=cfg.subtitle.wraplength, justify=justify,
            )
            y += h + gap

    def _show(self, text: str, *, new_line: bool = True):
        """Draw a subtitle; a revision of the same subtitle (new_line=False) keeps the previous line."""
        if self._root is None:
            return
        text = text.strip()
        if not text:
            return
        try:
            wrapped = "\n".join(textwrap.wrap(text, width=cfg.subtitle.max_width_chars)) or text
            if new_line and self._current_line and self._current_line != wrapped:
                self._previous_line = self._current_line
            self._current_line = wrapped
            self._visible_text = wrapped
            self._draw_outlined_text(wrapped)
            log.debug("Subtitle: %s", wrapped)
            self._show_time = time.monotonic()
            self._show_min_ms = max(
                cfg.subtitle.min_display_ms,
                len(text) * cfg.subtitle.ms_per_char,
            )
            self._cancel_hide()
            self._hide_job = self._root.after(cfg.subtitle.idle_hide_ms, self._hide)
        except tk.TclError:
            pass

    def _hide(self):
        self._visible_text = ""
        # After a quiet spell the next line starts fresh instead of under a stale one.
        self._current_line = ""
        self._previous_line = ""
        try:
            if self._history_until:
                pass  # an earlier subtitle's hide timer: leave the history view alone
            elif self._status_labels:
                self._draw_outlined_text("")  # keep the status line visible
            else:
                self._clear_canvas()
        except tk.TclError:
            pass
        self._hide_job = None

    def _quit(self):
        self._flush_marks()
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
