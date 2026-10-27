"""Small window for scripts/subtitle_url.py: paste one or more URLs, press 開始.

The job runs scripts/subtitle_url.py as a child process, so download, translation,
cost guards and file layout are exactly the command-line tool's; this file only
collects the options, queues URLs, shows progress, and asks before a costly
transcription or a retranslation into an existing folder.
"""
import json
import os
import queue
import re
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
TRANSLATORS = [("Gemini 3.8 Flash（預設）", "gemini"), ("DeepSeek", "deepseek")]
_URL = re.compile(r"https?://\S+")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def profile_choices() -> list[tuple[str, str]]:
    """(label, profile id) for the dropdown; "" is no profile."""
    try:
        data = json.loads((ROOT / "data" / "streamer_profiles.json").read_text(encoding="utf-8"))
        profiles = [(p.get("label") or p["profile_id"], p["profile_id"])
                    for p in data.get("profiles", []) if p.get("profile_id")]
    except (OSError, ValueError, KeyError):
        profiles = []
    return [("不指定", "")] + profiles


def parse_urls(text: str) -> list[str]:
    """Every http(s) URL in the field, in order, without repeats."""
    return list(dict.fromkeys(_URL.findall(text or "")))


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
                  assume_yes: bool, background: str = "", singers: str = "",
                  translator: str = "gemini", retranslate: bool = False) -> list[str]:
    command = [console_python(), str(TOOL), url, "--no-open", "--out", out]
    if lyrics:
        command.append("--lyrics")
    # "--opt=value": a note or name starting with "-" is not read as an option
    if background.strip():
        command.append(f"--background={background.strip()}")
    if singers.strip():
        command.append(f"--singers={singers.strip()}")
    if translator != "gemini":
        command += ["--translator", translator]
    if profile:
        command += ["--profile", profile]
    if prefer_audio:
        command.append("--prefer-audio")
    if retranslate:
        command.append("--retranslate")
    if assume_yes:
        command.append("--yes")
    return command


def info_command(url: str, out: str) -> list[str]:
    return [console_python(), str(TOOL), url, "--info", "--out", out]


def parse_info(output: str) -> dict | None:
    for line in reversed(output.splitlines()):
        if line.startswith('{"info"'):
            try:
                return json.loads(line)["info"]
            except (ValueError, KeyError):
                return None
    return None


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
                       creationflags=_NO_WINDOW)
    else:
        process.terminate()


def _child_env() -> dict:
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.output_dir: str | None = None
        self.last_error = ""
        self.pending: list[str] = []
        self.current = ""
        self.total = 0
        self.succeeded: list[str] = []
        self.failed: list[str] = []
        self.options: dict = {}
        self.info_running = False
        root.title("影片字幕翻譯")
        root.geometry("680x700")
        root.minsize(560, 600)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        hint = {"foreground": "gray"}

        ttk.Label(frame, text="影片網址").grid(row=0, column=0, sticky="w")
        self.url = tk.StringVar(value=self._clipboard_urls())
        entry = ttk.Entry(frame, textvariable=self.url)
        entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(4, 0))
        entry.focus_set()
        ttk.Label(frame, text="可一次貼多個網址（空白分隔），會依序處理，下面的設定套用到每一支", **hint).grid(
            row=1, column=1, columnspan=2, sticky="w")

        self.lyrics = tk.BooleanVar()
        ttk.Checkbutton(frame, text="這是歌曲（照歌詞翻譯）", variable=self.lyrics).grid(
            row=2, column=1, sticky="w", pady=(4, 0))
        self.prefer_audio = tk.BooleanVar()
        ttk.Checkbutton(frame, text="不用內建字幕，改用語音辨識", variable=self.prefer_audio).grid(
            row=3, column=1, sticky="w")

        ttk.Label(frame, text="主播").grid(row=4, column=0, sticky="w")
        self.profiles = profile_choices()
        self.profile = ttk.Combobox(frame, state="readonly", values=[label for label, _ in self.profiles])
        self.profile.current(0)
        self.profile.grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="翻譯模型").grid(row=5, column=0, sticky="w")
        self.translator = ttk.Combobox(frame, state="readonly", values=[label for label, _ in TRANSLATORS])
        self.translator.current(0)
        self.translator.grid(row=5, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="背景說明").grid(row=6, column=0, sticky="nw", pady=4)
        self.background = tk.Text(frame, height=5, wrap="word", undo=True)
        self.background.grid(row=6, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(frame, text="選填，給翻譯模型參考：誰在唱、故事、氣氛、名字寫法（標題會自動附上）", **hint).grid(
            row=7, column=1, sticky="w")
        self.info_button = ttk.Button(frame, text="帶入影片說明", command=self.fetch_info)
        self.info_button.grid(row=7, column=2, sticky="e")

        ttk.Label(frame, text="演唱者").grid(row=8, column=0, sticky="w")
        self.singers = tk.StringVar()
        ttk.Entry(frame, textvariable=self.singers).grid(row=8, column=1, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(frame, text="選填，字幕有分顏色時，依顏色出現順序填名字，用逗號分隔，例如：成員A, 成員B",
                  **hint).grid(row=9, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="輸出資料夾").grid(row=10, column=0, sticky="w")
        self.out = tk.StringVar(value=str(DEFAULT_OUT))
        ttk.Entry(frame, textvariable=self.out).grid(row=10, column=1, sticky="ew", pady=4)
        ttk.Button(frame, text="瀏覽…", command=self._pick_folder).grid(row=10, column=2, padx=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.start_button = ttk.Button(buttons, text="開始", command=self.start)
        self.start_button.pack(side="left")
        self.open_button = ttk.Button(buttons, text="開啟資料夾", command=self._open_folder, state="disabled")
        self.open_button.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=1.0)
        self.progress.grid(row=12, column=0, columnspan=3, sticky="ew", pady=4)
        self.log = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.log.grid(row=13, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(13, weight=1)
        # only the URL field: Enter in the background box is a new line, not a start
        entry.bind("<Return>", lambda _e: self.start())
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(100, self._drain)

    def _clipboard_urls(self) -> str:
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return ""
        urls = parse_urls(text)
        # only when the clipboard is nothing but links, not a paragraph that mentions one
        return " ".join(urls) if urls and not _URL.sub("", text).strip() else ""

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

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _out_dir(self) -> str:
        return self.out.get().strip() or str(DEFAULT_OUT)

    def _busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy or self.info_running else "normal")
        # one description fetch at a time: two would both write into the background box
        self.info_button.configure(state="disabled" if busy or self.info_running else "normal")

    # ---- video description -> background note -------------------------------------------
    def fetch_info(self) -> None:
        urls = parse_urls(self.url.get())
        if not urls:
            messagebox.showwarning("影片字幕翻譯", "請先貼上影片網址")
            return
        self.info_running = True
        self._busy(True)
        self._write("讀取影片說明…")
        threading.Thread(target=self._info_worker, args=(urls[0], self._out_dir()), daemon=True).start()

    def _info_worker(self, url: str, out: str) -> None:
        try:
            result = subprocess.run(info_command(url, out), cwd=ROOT, capture_output=True,
                                    stdin=subprocess.DEVNULL, env=_child_env(),
                                    creationflags=_NO_WINDOW, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            self.events.put(("info_error", "[失敗] 讀取影片資訊逾時或無法啟動"))
            return
        summary = parse_info(result.stdout.decode("utf-8", errors="replace"))
        if summary is None:
            errors = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            self.events.put(("info_error", next((e for e in reversed(errors) if e.startswith("[失敗]")),
                                                "[失敗] 讀取影片資訊失敗")))
        else:
            self.events.put(("info", summary))

    def _on_info(self, summary: dict) -> None:
        self.info_running = False
        self._busy(self.process is not None)
        minutes, seconds = divmod(int(summary.get("duration") or 0), 60)
        facts = [f"影片：{summary.get('title', '')}（{minutes}:{seconds:02}）",
                 "有內建韓文字幕" if summary.get("korean_track") else "沒有內建韓文字幕（會用語音辨識）"]
        if summary.get("done"):
            facts.append("已經做過，再做會詢問是否重新翻譯")
        self._write("｜".join(facts))
        description = summary.get("description") or ""
        if not description:
            self._write("影片說明是空的（或只有連結），背景說明請自己填")
            return
        current = self.background.get("1.0", "end").strip()
        if current and not messagebox.askyesno("影片字幕翻譯", "背景說明已經有內容，要用影片說明取代嗎？"):
            return
        self.background.delete("1.0", "end")
        self.background.insert("1.0", description)
        self._write(f"已帶入影片說明（{len(description)} 字，連結和 hashtag 已去掉），可以再刪改")

    # ---- job queue --------------------------------------------------------------------
    def start(self) -> None:
        if self.process is not None or str(self.start_button.cget("state")) == "disabled":
            return
        urls = parse_urls(self.url.get())
        if not urls:
            messagebox.showwarning("影片字幕翻譯", "請貼上影片網址")
            return
        background = self.background.get("1.0", "end").strip()
        if len(background) > BACKGROUND_NOTE_LIMIT:
            messagebox.showwarning("影片字幕翻譯",
                                   f"背景說明最多 {BACKGROUND_NOTE_LIMIT} 字（目前 {len(background)} 字）")
            return
        # one snapshot for the whole queue: editing the window mid-queue does not change it
        self.options = dict(
            lyrics=self.lyrics.get(), profile=self.profiles[self.profile.current()][1],
            out=self._out_dir(), prefer_audio=self.prefer_audio.get(), background=background,
            singers=self.singers.get(), translator=TRANSLATORS[self.translator.current()][1])
        self.pending, self.total = urls, len(urls)
        self.succeeded, self.failed = [], []
        self.output_dir = None
        self.open_button.configure(state="disabled")
        self._clear_log()
        self._launch_next()

    def _launch_next(self) -> None:
        if not self.pending:
            self._finish_queue()
            return
        self._launch(self.pending.pop(0))

    def _launch(self, url: str, *, assume_yes: bool = False, retranslate: bool = False) -> None:
        self.current, self.last_error, self.output_dir = url, "", None
        command = build_command(url, assume_yes=assume_yes, retranslate=retranslate, **self.options)
        self._busy(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        if self.total > 1 and not (assume_yes or retranslate):
            done = self.total - len(self.pending)
            self._write(f"── 第 {done}/{self.total} 支：{url}")
        try:
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            env=_child_env(), creationflags=_NO_WINDOW)
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
                elif kind == "exit":
                    self._on_exit(value)
                elif kind == "info":
                    self._on_info(value)
                else:
                    self.info_running = False
                    self._busy(self.process is not None)
                    self._write(value)
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
        if code == 0 and self.output_dir:
            self.succeeded.append(self.output_dir)
            self._launch_next()
            return
        existing = finished_folder(self.last_error)
        if existing:
            if messagebox.askyesno("影片字幕翻譯",
                                   "這支影片已經做過了。\n要用目前的設定重新翻譯嗎？\n"
                                   "（舊檔會移到「舊版」資料夾；不會重做語音辨識）"):
                self._write("重新翻譯…")
                self._launch(self.current, retranslate=True)
                return
            self.succeeded.append(existing)        # nothing new, but its folder is still the result
            self._launch_next()
            return
        if NEEDS_CONFIRM in self.last_error:
            summary = self.last_error.replace("[失敗]", "").split("；")[0].strip()
            if messagebox.askyesno("影片字幕翻譯", f"{summary}。\n要繼續嗎？"):
                self._write("已確認，繼續…")
                self._launch(self.current, assume_yes=True)
                return
        self.failed.append(self.current)
        self._launch_next()

    def _finish_queue(self) -> None:
        self._busy(False)
        self.progress.configure(mode="determinate", value=1.0 if self.succeeded else 0.0)
        if self.total > 1:
            self._write(f"── 全部結束：完成 {len(self.succeeded)} 支，失敗 {len(self.failed)} 支")
            for url in self.failed:
                self._write(f"   失敗：{url}")
        if self.succeeded:
            self.output_dir = self.succeeded[-1]
            self.open_button.configure(state="normal")
            if self.total == 1:
                self._open_folder()

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
