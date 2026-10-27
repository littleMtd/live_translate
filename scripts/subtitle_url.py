"""Korean video URL -> Traditional Chinese subtitle files, in one command.

    python scripts/subtitle_url.py <url> [--lyrics] [--background TEXT] [--out DIR] [--profile ID] [--yes]

Uses the video's own Korean subtitles when it has them (more accurate than speech
recognition, and no STT cost); otherwise downloads only the audio and transcribes it.
Translation and file output go through scripts/make_subtitles.py, so cost, quality
report and file layout are the same as the cafe_fansite "下載＋字幕" job.
Files land in <out>/<title> [<id>]/ ; the folder opens when the job is done.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_OUT = Path.home() / "Downloads" / "字幕"
KOREAN_TRACKS = ("ko", "ko-KR", "ko-kr")
# Speech recognition beyond this needs a confirmation (or --yes): a 3-hour VOD is real money.
LONG_TRANSCRIBE_SECONDS = 30 * 60
STT_USD_PER_HOUR = 0.27  # ElevenLabs Scribe v2 + keyterms, batch price checked 2026-10-09
# Owner note cap: offline_subtitles.BACKGROUND_LIMIT (1500) minus room for the title line
NOTE_LIMIT = 1200
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


class ToolError(Exception):
    """A message for the owner; printed without a traceback."""


def safe_name(text: str, limit: int = 80) -> str:
    """A Windows-safe file/folder name: no reserved characters or device names."""
    name = _UNSAFE.sub(" ", text or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)[:limit].strip().strip(".")
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name or "video"


def korean_track(subtitles: dict) -> str | None:
    """The uploader's Korean subtitle track, if any. Auto-generated captions are ignored."""
    for key in KOREAN_TRACKS:
        if subtitles.get(key):
            return key
    return next((key for key in subtitles if key.lower().startswith("ko")), None)


def _ydl(options: dict):
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "ignoreconfig": True}
    return yt_dlp.YoutubeDL({**base, **options})


def _download_errors():
    from yt_dlp.utils import DownloadError, ExtractorError

    return DownloadError, ExtractorError


def fetch_info(url: str) -> dict:
    try:
        with _ydl({"skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)
    except _download_errors():
        raise ToolError("無法讀取這支影片（私人、年齡或地區限制、已刪除，或網址錯誤）") from None


# Temp files use a fixed name: titles such as "[MV] ..." are glob patterns.
def download_subtitle(url: str, track: str, workdir: Path) -> Path:
    options = {"skip_download": True, "writesubtitles": True, "subtitleslangs": [track],
               "subtitlesformat": "vtt", "outtmpl": str(workdir / "source")}
    try:
        with _ydl(options) as ydl:
            ydl.download([url])
    except _download_errors():
        raise ToolError("韓文字幕下載失敗") from None
    found = sorted(workdir.glob("source*.vtt"))
    if not found:
        raise ToolError("韓文字幕下載失敗")
    return found[0]


def download_audio(url: str, workdir: Path) -> Path:
    options = {"format": "bestaudio/best", "outtmpl": str(workdir / "source.%(ext)s"),
               "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        options["ffmpeg_location"] = str(Path(ffmpeg).parent)
    try:
        with _ydl(options) as ydl:
            ydl.download([url])
    except _download_errors():
        raise ToolError("音訊下載失敗") from None
    found = workdir / "source.m4a"
    if not found.is_file():
        raise ToolError("音訊下載失敗（需要 ffmpeg）")
    return found


def plan(info: dict, prefer_audio: bool) -> tuple[str, str | None]:
    """('translate', track) when the video has a Korean subtitle track, else ('transcribe', None)."""
    track = None if prefer_audio else korean_track(info.get("subtitles") or {})
    return ("translate", track) if track else ("transcribe", None)


def check_video(info: dict) -> None:
    if info.get("_type", "video") != "video":
        raise ToolError("這是播放清單，請貼單一影片的網址")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise ToolError("直播中或尚未開始的直播無法處理，請等存檔完成後再試")


def confirm_long_transcribe(duration: float, assume_yes: bool) -> None:
    if assume_yes or 0 < duration <= LONG_TRANSCRIBE_SECONDS:
        return
    if duration > 0:
        minutes, cost = duration / 60, duration / 3600 * STT_USD_PER_HOUR
        summary = f"影片 {minutes:.0f} 分鐘，語音辨識約 {cost:.2f} 美元（不含翻譯）"
    else:
        summary = f"影片長度不明，語音辨識每小時約 {STT_USD_PER_HOUR:.2f} 美元"
    prompt = f"{summary}。繼續？(y/N): "
    if not sys.stdin or not sys.stdin.isatty():
        raise ToolError(f"{summary}；確定要做請加 --yes")
    if input(prompt).strip().lower() != "y":
        raise ToolError("已取消")


def background_note(title: str, note: str) -> str:
    """What the translator sees about the video: its title, then the owner's own note."""
    from modules.offline_subtitles import BACKGROUND_LIMIT

    note = (note or "").strip()
    text = f"影片標題：{title}" + (f"\n{note}" if note else "")
    if len(text) > BACKGROUND_LIMIT:
        raise ToolError(f"背景說明太長了（連同標題最多 {BACKGROUND_LIMIT} 字）")
    return text


_LINK_LINE = re.compile(r"https?://|www\.|@\w", re.I)
_TAGS_LINE = re.compile(r"^(#\S+\s*)+$")


def clean_description(text: str, limit: int) -> str:
    """The video description as a starting background note: links and hashtag lines dropped."""
    kept = []
    for line in (text or "").splitlines():
        line = line.strip()
        if _LINK_LINE.search(line) or _TAGS_LINE.match(line):
            continue
        if line or (kept and kept[-1]):
            kept.append(line)
    out = ""
    for line in kept:
        room = limit - len(out) - 1
        if len(line) > room:
            # a long single paragraph is cut, not dropped
            out += line[:max(room, 0)]
            break
        out += line + "\n"
    return out.strip()


def video_summary(info: dict, out: Path) -> dict:
    """What the GUI needs before a job: no download, no paid call."""
    title, video_id = info.get("title") or "video", info.get("id") or "unknown"
    out_dir = out / f"{safe_name(title)} [{video_id}]"
    return {"title": title, "duration": info.get("duration") or 0,
            "korean_track": bool(korean_track(info.get("subtitles") or {})),
            "done": _has_files(out_dir),
            "description": clean_description(info.get("description") or "", NOTE_LIMIT)}


def _has_files(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_file() for p in folder.iterdir())


def replace_outputs(out_dir: Path, new_dir: Path, stamp: str) -> Path:
    """Move the previous files into 舊版/<stamp>/ and the new ones (staged in a folder inside
    out_dir, so every move is a same-volume rename) in. os.replace never falls back to
    copy-then-delete, so a locked file fails cleanly; a failure is undone best-effort and
    the new files stay in new_dir for the owner."""
    archive = out_dir / "舊版" / stamp
    n = 1
    while archive.exists():
        archive, n = out_dir / "舊版" / f"{stamp}-{n}", n + 1
    moved = []
    try:
        archive.mkdir(parents=True)
        for old in sorted(p for p in out_dir.iterdir() if p.is_file()):
            os.replace(old, archive / old.name)
            moved.append((archive / old.name, old))
        for new in sorted(new_dir.iterdir()):
            os.replace(new, out_dir / new.name)
            moved.append((out_dir / new.name, new))
    except OSError:
        for now, before in reversed(moved):
            try:
                os.replace(now, before)
            except OSError:
                pass
        for folder in (archive, archive.parent):
            try:
                folder.rmdir()               # only when empty
            except OSError:
                pass
        raise ToolError(f"有檔案正在被其他程式開啟，無法替換：{out_dir}（關掉後再試；"
                        f"新的翻譯在 {new_dir.name}）") from None
    new_dir.rmdir()
    return archive


def previous_was_transcribed(out_dir: Path, stem: str) -> bool:
    """Whether the folder's last job came from speech recognition (its report names an STT provider)."""
    try:
        report = json.loads((out_dir / f"{stem}.report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(report, dict) and bool(report.get("stt_provider"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Korean video URL -> zh-TW subtitles")
    parser.add_argument("url")
    parser.add_argument("--lyrics", action="store_true", help="song: translate as lyric lines")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"default {DEFAULT_OUT}")
    parser.add_argument("--profile", default="", help="streamer profile for names (default: none)")
    parser.add_argument("--translator", choices=("gemini", "deepseek"), default="gemini")
    parser.add_argument("--prefer-audio", action="store_true",
                        help="transcribe the audio even when the video has Korean subtitles")
    parser.add_argument("--background", default="",
                        help="note for the translator: singers, story, tone (the title is added automatically)")
    parser.add_argument("--singers", default="",
                        help="names for the singer colours, comma-separated in order of first appearance")
    parser.add_argument("--retranslate", action="store_true",
                        help="translate again into an existing folder; old files move to 舊版/, "
                             "speech recognition is never repeated")
    parser.add_argument("--info", action="store_true",
                        help="print the video's title, length and cleaned description as JSON, then stop")
    parser.add_argument("--yes", action="store_true", help="skip the long-video cost confirmation")
    parser.add_argument("--no-open", action="store_true", help="do not open the folder when done")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ToolError as exc:
        print(f"[失敗] {exc}", file=sys.stderr)
        return 1


def run(args) -> int:
    from modules.offline_subtitles import OfflineSubtitleError, parse_singers

    try:
        parse_singers(args.singers)        # checked before any download, not after
    except OfflineSubtitleError as exc:
        raise ToolError(f"演唱者名字有誤：{exc}") from None
    if not args.info:
        print("讀取影片資訊…", flush=True)
    info = fetch_info(args.url)
    check_video(info)
    if args.info:
        print(json.dumps({"info": video_summary(info, args.out)}, ensure_ascii=False), flush=True)
        return 0
    title, video_id = info.get("title") or "video", info.get("id") or "unknown"
    stem = safe_name(title)
    background = background_note(title, args.background)
    out_dir = args.out / f"{stem} [{video_id}]"
    retranslate = args.retranslate and _has_files(out_dir)
    if _has_files(out_dir) and not retranslate:
        raise ToolError(f"這支影片已經做過了：{out_dir}（要重做請先刪掉或改 --out）")
    mode, track = plan(info, args.prefer_audio)
    previous = out_dir / f"{stem}.ko.vtt"
    if retranslate:
        # Keep the source of the last job: its uploader track again (free, and the saved
        # ko.vtt has no singer colours), or its speech-recognition result, never STT again.
        track = None if previous_was_transcribed(out_dir, stem) else korean_track(info.get("subtitles") or {})
        mode = "translate"
        if not track and not previous.is_file():
            raise ToolError(f"找不到上次的韓文字幕 {previous.name}，無法只重新翻譯")
    print(f"影片：{title}（{info.get('duration_string') or '?'}）", flush=True)
    if retranslate:
        print("重新翻譯：" + ("重新抓影片內建的韓文字幕" if track else "沿用上次的韓文字幕"), flush=True)
    else:
        print("使用影片內建的韓文字幕翻譯" if track else "沒有內建韓文字幕，下載音訊做語音辨識", flush=True)
    if mode == "transcribe":
        confirm_long_transcribe(float(info.get("duration") or 0), args.yes)

    import make_subtitles

    with tempfile.TemporaryDirectory(prefix="subtitle_url_") as temp:
        workdir = Path(temp)
        if track:
            source = download_subtitle(args.url, track, workdir)
        elif retranslate:
            source = Path(shutil.copy2(previous, workdir / "source.vtt"))
        else:
            source = download_audio(args.url, workdir)
        # make_subtitles names its outputs after the input file's stem
        source = source.rename(workdir / f"{stem}{source.suffix}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # staged inside out_dir: replacing is then a same-volume rename even when --out is on another drive
        job_out = out_dir / f"新版暫存-{stamp}" if retranslate else out_dir
        # "--opt=value": a value starting with "-" is not read as an option
        job_args = [mode, "--input", str(source), "--out-dir", str(job_out),
                    "--profile", args.profile, "--translator", args.translator,
                    f"--background={background}", f"--singers={args.singers}"]
        if args.lyrics:
            job_args.append("--lyrics")
        print("翻譯中…（進度為 JSON 行）", flush=True)
        status = make_subtitles.main(job_args)
        if retranslate and status != 0:
            shutil.rmtree(job_out, ignore_errors=True)   # a failed job leaves nothing worth keeping
        if status == 0 and retranslate:
            archive = replace_outputs(out_dir, job_out, stamp)
            print(f"上一版移到：{archive}", flush=True)
    if status != 0:
        raise ToolError("字幕製作失敗，原因見上方訊息")
    print(f"完成：{out_dir}", flush=True)
    if not args.no_open and sys.platform == "win32":
        os.startfile(out_dir)  # noqa: S606 - opens the output folder for the owner
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
