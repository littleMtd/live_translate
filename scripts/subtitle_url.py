"""Korean video URL -> Traditional Chinese subtitle files, in one command.

    python scripts/subtitle_url.py <url> [--lyrics] [--background TEXT] [--out DIR] [--profile ID] [--yes]

Uses the video's own Korean subtitles when it has them (more accurate than speech
recognition, and no STT cost); otherwise downloads only the audio and transcribes it.
Translation and file output go through scripts/make_subtitles.py, so cost, quality
report and file layout are the same as the cafe_fansite "下載＋字幕" job.
Files land in <out>/<title> [<id>]/ ; the folder opens when the job is done.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_OUT = Path.home() / "Downloads" / "字幕"
KOREAN_TRACKS = ("ko", "ko-KR", "ko-kr")
# Speech recognition beyond this needs a confirmation (or --yes): a 3-hour VOD is real money.
LONG_TRANSCRIBE_SECONDS = 30 * 60
STT_USD_PER_HOUR = 0.27  # ElevenLabs Scribe v2 + keyterms, batch price checked 2026-10-09
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
    parser.add_argument("--yes", action="store_true", help="skip the long-video cost confirmation")
    parser.add_argument("--no-open", action="store_true", help="do not open the folder when done")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ToolError as exc:
        print(f"[失敗] {exc}", file=sys.stderr)
        return 1


def run(args) -> int:
    print("讀取影片資訊…", flush=True)
    info = fetch_info(args.url)
    check_video(info)
    title, video_id = info.get("title") or "video", info.get("id") or "unknown"
    stem = safe_name(title)
    background = background_note(title, args.background)
    out_dir = args.out / f"{stem} [{video_id}]"
    if out_dir.is_dir() and any(out_dir.iterdir()):
        raise ToolError(f"這支影片已經做過了：{out_dir}（要重做請先刪掉或改 --out）")
    mode, track = plan(info, args.prefer_audio)
    print(f"影片：{title}（{info.get('duration_string') or '?'}）", flush=True)
    print("使用影片內建的韓文字幕翻譯" if track else "沒有內建韓文字幕，下載音訊做語音辨識", flush=True)
    if mode == "transcribe":
        confirm_long_transcribe(float(info.get("duration") or 0), args.yes)

    import make_subtitles

    with tempfile.TemporaryDirectory(prefix="subtitle_url_") as temp:
        workdir = Path(temp)
        source = download_subtitle(args.url, track, workdir) if track else download_audio(args.url, workdir)
        # make_subtitles names its outputs after the input file's stem
        source = source.rename(workdir / f"{stem}{source.suffix}")
        job_args = [mode, "--input", str(source), "--out-dir", str(out_dir),
                    "--profile", args.profile, "--translator", args.translator,
                    "--background", background]
        if args.lyrics:
            job_args.append("--lyrics")
        print("翻譯中…（進度為 JSON 行）", flush=True)
        status = make_subtitles.main(job_args)
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
