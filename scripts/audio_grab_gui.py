"""Small window for scripts/audio_grab.py: audio only, from a URL or from what the computer plays.

The job runs scripts/audio_grab.py as a child process (same reasons as subtitle_gui.py:
no console windows from ffmpeg, and the window never freezes). 停止 creates the
child's --stop-file, so the recording is closed and saved properly.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subtitle_gui import _URL, console_python, parse_urls, stop_tree  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "audio_grab.py"
DEFAULT_OUT = Path.home() / "Downloads" / "錄音"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TITLE = "只錄聲音"


def device_label(device: dict) -> str:
    if device["kind"] == "speaker":
        return f"{device['name']}（{'你現在聽到的聲音' if device['default'] else '這個輸出的聲音'}）"
    return f"{device['name']}（輸入）"


def build_command(mode: str, *, url: str = "", device: str = "", kind: str = "", out: str,
                  stop_file: str) -> list[str]:
    command = [console_python(), str(TOOL), mode]
    if mode == "url":
        command.append(url)
    elif device:
        command.append(f"--device={device}")
        if kind:
            # a Bluetooth headset's speaker and microphone can share one name
            command.append(f"--kind={kind}")
    return command + ["--out", out, "--stop-file", stop_file]


def clock(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}:{rest // 60:02}:{rest % 60:02}" if hours else f"{rest // 60}:{rest % 60:02}"


def describe(line: str) -> tuple[str | None, float | None, float | None, str | None]:
    """A child output line -> (status text, progress 0..1, level 0..1, finished file)."""
    line = line.strip()
    if not line:
        return None, None, None, None
    if not line.startswith("{"):
        if line.startswith("完成："):
            return line, 1.0, None, line[len("完成："):].strip()
        return line, None, None, None
    try:
        event = json.loads(line)
    except ValueError:
        return None, None, None, None
    stage = event.get("stage")
    if stage == "download":
        done, total = event.get("done", 0), event.get("total") or 0
        mb = f"{done / 1e6:.1f}" + (f" / {total / 1e6:.1f}" if total else "") + " MB"
        return f"下載中 {mb}", (done / total if total else None), None, None
    if stage == "live":
        return (f"直播錄音中 {clock(event.get('seconds', 0))}（{event.get('bytes', 0) / 1e6:.1f} MB）",
                None, None, None)
    if stage == "record":
        level = float(event.get("level") or 0)
        quiet = "（沒有聲音）" if level < 0.003 else ""
        return f"錄音中 {clock(event.get('seconds', 0))}{quiet}", None, min(level, 1.0), None
    return None, None, None, None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.stop_file: Path | None = None
        self.output: str | None = None
        self.devices: list[dict] = []
        root.title(TITLE)
        root.geometry("600x440")
        root.minsize(500, 380)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        hint = {"foreground": "gray"}

        self.mode = tk.StringVar(value="url")
        modes = ttk.Frame(frame)
        modes.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Radiobutton(modes, text="貼網址（影片或直播）", value="url", variable=self.mode,
                        command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(modes, text="錄電腦正在播放的聲音", value="record", variable=self.mode,
                        command=self._mode_changed).pack(side="left", padx=12)

        ttk.Label(frame, text="網址").grid(row=1, column=0, sticky="w")
        self.url = tk.StringVar(value=self._clipboard_url())
        self.url_entry = ttk.Entry(frame, textvariable=self.url)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        self.url_hint = ttk.Label(frame, text="保留網站原本的音軌，不轉檔不失真；直播會一直錄到你按停止或直播結束", **hint)
        self.url_hint.grid(row=2, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="錄哪裡").grid(row=3, column=0, sticky="w")
        self.device = ttk.Combobox(frame, state="readonly", values=["讀取裝置中…"])
        self.device.current(0)
        self.device.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        self.device_hint = ttk.Label(frame, text="存成無損 FLAC；其他程式的聲音（通知、Discord）也會一起錄進去", **hint)
        self.device_hint.grid(row=4, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="存到").grid(row=5, column=0, sticky="w")
        self.out = tk.StringVar(value=str(DEFAULT_OUT))
        ttk.Entry(frame, textvariable=self.out).grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Button(frame, text="瀏覽…", command=self._pick_folder).grid(row=5, column=2, padx=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.start_button = ttk.Button(buttons, text="開始", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止並存檔", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        self.open_button = ttk.Button(buttons, text="開啟資料夾", command=self._open_folder)
        self.open_button.pack(side="left")

        self.status = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky="w")
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=1.0)
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=4)
        self.log = tk.Text(frame, height=8, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(9, weight=1)
        self.url_entry.bind("<Return>", lambda _e: self.start())
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._mode_changed()
        threading.Thread(target=self._load_devices, daemon=True).start()
        root.after(100, self._drain)

    # ---- small helpers ----------------------------------------------------------------
    def _clipboard_url(self) -> str:
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return ""
        urls = parse_urls(text)
        return urls[0] if len(urls) == 1 and not _URL.sub("", text).strip() else ""

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.out.get() or str(DEFAULT_OUT))
        if chosen:
            self.out.set(chosen)

    def _open_folder(self) -> None:
        target = Path(self.output).parent if self.output else Path(self.out.get() or DEFAULT_OUT)
        if target.is_dir():
            os.startfile(target)  # noqa: S606 - the owner's own output folder

    def _write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _mode_changed(self) -> None:
        url_mode = self.mode.get() == "url"
        self.url_entry.configure(state="normal" if url_mode else "disabled")
        self.device.configure(state="disabled" if url_mode else "readonly")

    def _load_devices(self) -> None:
        try:
            result = subprocess.run([console_python(), str(TOOL), "devices"], cwd=ROOT, capture_output=True,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                    creationflags=_NO_WINDOW, timeout=30)
            self.events.put(("devices", json.loads(result.stdout.decode("utf-8"))))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.events.put(("devices", []))

    # ---- job ----------------------------------------------------------------------------
    def start(self) -> None:
        if self.process is not None:
            return
        mode, out = self.mode.get(), self.out.get().strip() or str(DEFAULT_OUT)
        url, device, kind = "", "", ""
        if mode == "url":
            urls = parse_urls(self.url.get())
            if len(urls) != 1:
                messagebox.showwarning(TITLE, "請貼上一個影片或直播的網址")
                return
            url = urls[0]
        else:
            if not self.devices:
                messagebox.showwarning(TITLE, "還沒讀到可以錄的裝置")
                return
            chosen = self.devices[self.device.current()]
            device, kind = chosen["name"], chosen["kind"]
        self.stop_file = Path(tempfile.gettempdir()) / f"audio_grab_{os.getpid()}_{id(self)}.stop"
        self.stop_file.unlink(missing_ok=True)
        command = build_command(mode, url=url, device=device, kind=kind, out=out, stop_file=str(self.stop_file))
        self.output = None
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.status.set("準備中…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except OSError as exc:
            self._write(f"[失敗] 無法啟動：{exc}")
            self._finish(1)
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()

    def stop(self) -> None:
        if self.process is not None and self.stop_file is not None:
            self.stop_file.write_text("", encoding="utf-8")
            self.stop_button.configure(state="disabled")
            self.status.set("存檔中…")

    def _pump(self, process: subprocess.Popen) -> None:
        for raw in process.stdout:
            self.events.put(("line", raw.decode("utf-8", errors="replace")))
        self.events.put(("exit", process.wait()))

    def _drain(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "line":
                    self._on_line(value)
                elif kind == "exit":
                    self._finish(value)
                else:
                    self._on_devices(value)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _on_devices(self, found: list[dict]) -> None:
        self.devices = found
        self.device.configure(values=[device_label(d) for d in found] or ["找不到可以錄的裝置"])
        self.device.current(0)
        self._mode_changed()

    def _on_line(self, line: str) -> None:
        text, fraction, level, finished = describe(line)
        if finished:
            self.output = finished
        if fraction is not None or level is not None:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=fraction if fraction is not None else level)
        if not text:
            return
        if line.lstrip().startswith("{"):
            self.status.set(text)           # progress replaces itself instead of filling the log
        else:
            self._write(text)

    def _finish(self, code: int) -> None:
        self.process = None
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=1.0 if code == 0 and self.output else 0.0)
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.set("完成" if code == 0 and self.output else "沒有存檔")

    def _close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno(TITLE, "還在錄，要停止並存檔再關閉嗎？"):
                return
            self.stop()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                stop_tree(self.process)
            if self.stop_file is not None:
                self.stop_file.unlink(missing_ok=True)   # _finish never runs after destroy
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
