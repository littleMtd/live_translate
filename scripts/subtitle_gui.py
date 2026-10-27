"""Small window for scripts/subtitle_url.py: paste a URL, press 開始.

The job runs scripts/subtitle_url.py as a child process, so download, translation,
cost guards and file layout are exactly the command-line tool's; this file only
collects the options, shows progress, and asks before a costly transcription.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "subtitle_url.py"
DEFAULT_OUT = Path.home() / "Downloads" / "字幕"
NEEDS_CONFIRM = "--yes"          # subtitle_url's message when a long transcription needs consent
_JSON_PREFIX = "{"
# offline_subtitles.BACKGROUND_LIMIT (1500) minus room for the title subtitle_url adds
BACKGROUND_NOTE_LIMIT = 1200


def profile_choices() -> list[tuple[str, str]]:
    """(label, profile id) for the dropdown; "" is no profile."""
    try:
        data = json.loads((ROOT / "data" / "streamer_profiles.json").read_text(encoding="utf-8"))
        profiles = [(p.get("label") or p["profile_id"], p["profile_id"])
                    for p in data.get("profiles", []) if p.get("profile_id")]
    except (OSError, ValueError, KeyError):
        profiles = []
    return [("不指定", "")] + profiles


def console_python() -> str:
    """python.exe, not pythonw.exe: a windowless parent's child has no console, so every
    ffmpeg/ffprobe it starts would pop up its own console window. Started with
    CREATE_NO_WINDOW, python.exe gets a hidden console that ffmpeg inherits."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        sibling = executable.with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    return str(executable)


def build_command(url: str, *, lyrics: bool, profile: str, out: str, prefer_audio: bool,
                  assume_yes: bool, background: str = "") -> list[str]:
    command = [console_python(), str(TOOL), url, "--no-open", "--out", out]
    if lyrics:
        command.append("--lyrics")
    if background.strip():
        command += ["--background", background.strip()]
    if profile:
        command += ["--profile", profile]
    if prefer_audio:
        command.append("--prefer-audio")
    if assume_yes:
        command.append("--yes")
    return command


def describe(line: str) -> tuple[str | None, float | None, str | None]:
    """A child output line -> (status text, progress 0..1, finished folder)."""
    line = line.strip()
    if not line:
        return None, None, None
    if not line.startswith(_JSON_PREFIX):
        if line.startswith("完成："):
            return line, 1.0, line[len("完成："):].strip()
        return line, None, None
    try:
        event = json.loads(line)
    except ValueError:
        return None, None, None
    stage = event.get("stage")
    if stage in ("translate", "transcribe") and event.get("total"):
        done, total = event.get("done", 0), event["total"]
        label = "翻譯" if stage == "translate" else "語音辨識"
        return f"{label} {done}/{total}", done / total, None
    if stage == "error":
        return f"[失敗] {event.get('message', '')}", None, None
    return None, None, None


def finished_folder(error_line: str) -> str | None:
    """The existing output folder named in subtitle_url's 'already done' message."""
    marker, tail = "已經做過了：", "（要重做"
    if marker not in error_line:
        return None
    # rsplit on the fixed tail: titles may contain a full-width （ themselves
    return error_line.split(marker, 1)[1].rsplit(tail, 1)[0].strip() or None


def stop_tree(process: subprocess.Popen) -> None:
    """End the job and anything it started (yt-dlp's ffmpeg); terminate() alone leaves them."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.terminate()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.output_dir: str | None = None
        self.last_error = ""
        root.title("影片字幕翻譯")
        root.geometry("640x540")
        root.minsize(540, 460)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="影片網址").grid(row=0, column=0, sticky="w")
        self.url = tk.StringVar(value=self._clipboard_url())
        entry = ttk.Entry(frame, textvariable=self.url)
        entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        entry.focus_set()

        self.lyrics = tk.BooleanVar()
        ttk.Checkbutton(frame, text="這是歌曲（照歌詞翻譯）", variable=self.lyrics).grid(
            row=1, column=1, sticky="w")
        self.prefer_audio = tk.BooleanVar()
        ttk.Checkbutton(frame, text="不用內建字幕，改用語音辨識", variable=self.prefer_audio).grid(
            row=2, column=1, sticky="w")

        ttk.Label(frame, text="主播").grid(row=3, column=0, sticky="w")
        self.profiles = profile_choices()
        self.profile = ttk.Combobox(frame, state="readonly", values=[label for label, _ in self.profiles])
        self.profile.current(0)
        self.profile.grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="背景說明").grid(row=4, column=0, sticky="nw", pady=4)
        self.background = tk.Text(frame, height=4, wrap="word", undo=True)
        self.background.grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(frame, text="選填，給翻譯模型參考：誰在唱、故事、氣氛、名字寫法（標題會自動附上）",
                  foreground="gray").grid(row=5, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="輸出資料夾").grid(row=6, column=0, sticky="w")
        self.out = tk.StringVar(value=str(DEFAULT_OUT))
        ttk.Entry(frame, textvariable=self.out).grid(row=6, column=1, sticky="ew", pady=4)
        ttk.Button(frame, text="瀏覽…", command=self._pick_folder).grid(row=6, column=2, padx=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.start_button = ttk.Button(buttons, text="開始", command=self.start)
        self.start_button.pack(side="left")
        self.open_button = ttk.Button(buttons, text="開啟資料夾", command=self._open_folder, state="disabled")
        self.open_button.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=1.0)
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=4)
        self.log = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(9, weight=1)
        # only the URL field: Enter in the background box is a new line, not a start
        entry.bind("<Return>", lambda _e: self.start())
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(100, self._drain)

    def _clipboard_url(self) -> str:
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return ""
        return text if text.startswith(("http://", "https://")) and "\n" not in text else ""

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.out.get() or str(DEFAULT_OUT))
        if chosen:
            self.out.set(chosen)

    def _open_folder(self) -> None:
        if self.output_dir and Path(self.output_dir).is_dir():
            os.startfile(self.output_dir)  # noqa: S606 - the owner's own output folder

    def _write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self, assume_yes: bool = False) -> None:
        if self.process is not None:
            return
        url = self.url.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("影片字幕翻譯", "請貼上影片網址")
            return
        background = self.background.get("1.0", "end").strip()
        if len(background) > BACKGROUND_NOTE_LIMIT:
            messagebox.showwarning("影片字幕翻譯",
                                   f"背景說明最多 {BACKGROUND_NOTE_LIMIT} 字（目前 {len(background)} 字）")
            return
        command = build_command(
            url, lyrics=self.lyrics.get(), profile=self.profiles[self.profile.current()][1],
            out=self.out.get().strip() or str(DEFAULT_OUT), prefer_audio=self.prefer_audio.get(),
            assume_yes=assume_yes, background=background)
        self.output_dir, self.last_error = None, ""
        self.open_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        if not assume_yes:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            env=env, creationflags=flags)
        except OSError as exc:
            self._write(f"[失敗] 無法啟動：{exc}")
            self._on_exit(1)
            return
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()

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
                else:
                    self._on_exit(value)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _on_line(self, line: str) -> None:
        text, fraction, folder = describe(line)
        if fraction is not None:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=fraction)
        if folder:
            self.output_dir = folder
        if text and not text.startswith("[download]"):
            self._write(text)
            if text.startswith("[失敗]"):
                self.last_error = text

    def _on_exit(self, code: int) -> None:
        self.process = None
        self.progress.stop()
        self.start_button.configure(state="normal")
        if code == 0 and self.output_dir:
            self.progress.configure(mode="determinate", value=1.0)
            self.open_button.configure(state="normal")
            self._open_folder()
            return
        self.progress.configure(mode="determinate", value=0.0)
        existing = finished_folder(self.last_error)
        if existing:
            self.output_dir = existing
            self.open_button.configure(state="normal")
        if NEEDS_CONFIRM in self.last_error:
            summary = self.last_error.replace("[失敗]", "").split("；")[0].strip()
            if messagebox.askyesno("影片字幕翻譯", f"{summary}。\n要繼續嗎？"):
                self._write("已確認，繼續…")
                self.start(assume_yes=True)

    def _close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno("影片字幕翻譯", "還在處理中，確定要關閉嗎？"):
                return
            stop_tree(self.process)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
