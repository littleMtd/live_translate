"""Audio only, no picture: from a video/live URL, or from what the computer is playing.

    python scripts/audio_grab.py url <url> [--out DIR]
    python scripts/audio_grab.py record [--device NAME] [--out DIR]
    python scripts/audio_grab.py devices

URL: the site's own audio track is kept as is (no re-encode, so no extra loss); when a
site only offers audio+video together, ffmpeg copies the audio out. A live stream is
recorded until the stream ends or you stop (Ctrl+C, or creating the --stop-file,
which the window uses). The file is always closed properly.
record: a speaker/headphone (what you hear) or an input such as CABLE Output, saved
as lossless FLAC: the captured sound is already PCM, so FLAC adds no loss.
Progress lines on stdout are JSON objects; other lines are plain status text.
"""
import argparse
import collections
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from subtitle_url import ToolError, _download_errors, _ydl, safe_name  # noqa: E402

DEFAULT_OUT = Path.home() / "Downloads" / "錄音"
SAMPLE_RATE = 48000
CHANNELS = 2
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# copy the audio stream into a container that suits its codec; ADTS .aac stays
# playable even if a live recording is cut off hard
_CONTAINERS = {"mp4a": ("aac", "adts"), "aac": ("aac", "adts"), "opus": ("opus", "ogg"),
               "mp3": ("mp3", "mp3")}


def report(stage: str, **fields) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    """folder/stem.suffix, or "stem (2).suffix" ... when that name is taken."""
    path, n = folder / f"{stem}{suffix}", 2
    while path.exists():
        path, n = folder / f"{stem} ({n}){suffix}", n + 1
    return path


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise ToolError("找不到 ffmpeg（網址模式需要它）")
    return found


def is_live(info: dict) -> bool:
    return bool(info.get("is_live")) or info.get("live_status") == "is_live"


# ---- URL ---------------------------------------------------------------------------------
def fetch_info(url: str) -> dict:
    try:
        with _ydl({"skip_download": True, "format": "bestaudio/best"}) as ydl:
            info = ydl.extract_info(url, download=False)
    except _download_errors():
        raise ToolError("無法讀取這個網址（私人、限制、已刪除，或網址錯誤）") from None
    if info.get("_type", "video") != "video":
        raise ToolError("這是播放清單，請貼單一影片或直播的網址")
    if info.get("live_status") == "is_upcoming":
        raise ToolError("直播還沒開始，開始後再試")
    return info


def stream_of(info: dict) -> dict:
    """The chosen format: audio-only when the site has one, else audio+video."""
    for fmt in info.get("requested_formats") or ():
        if fmt.get("acodec") not in (None, "none"):
            return fmt
    return info


def grab_vod(url: str, info: dict, out_dir: Path, stop: threading.Event) -> Path:
    title = safe_name(info.get("title") or "audio")
    stem = f"{title} [{info.get('id') or 'unknown'}]"

    def hook(status):
        if stop.is_set():
            from yt_dlp.utils import DownloadCancelled

            raise DownloadCancelled("stopped")
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            report("download", done=status.get("downloaded_bytes") or 0, total=total)

    with tempfile.TemporaryDirectory(prefix="audio_grab_") as temp:
        workdir = Path(temp)
        options = {"format": "bestaudio/best", "outtmpl": str(workdir / "source.%(ext)s"),
                   "progress_hooks": [hook], "ffmpeg_location": str(Path(ffmpeg_path()).parent),
                   # "best" copies the audio stream as is; only an audio+video download is touched
                   "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]}
        from yt_dlp.utils import DownloadCancelled

        try:
            with _ydl(options) as ydl:
                ydl.download([url])
        # DownloadCancelled (our stop) is not a DownloadError, so it needs its own branch
        except (DownloadCancelled, *_download_errors()):
            if stop.is_set():
                raise ToolError("已停止，沒有存檔") from None
            raise ToolError("下載失敗") from None
        found = [p for p in workdir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        if not found:
            raise ToolError("下載失敗（沒有產生音檔）")
        source = max(found, key=lambda p: p.stat().st_size)
        out_dir.mkdir(parents=True, exist_ok=True)
        final = unique_path(out_dir, stem, source.suffix)
        shutil.move(str(source), final)
    return final


def live_command(stream: dict, target: Path, container: str) -> list[str]:
    headers = "".join(f"{k}: {v}\r\n" for k, v in (stream.get("http_headers") or {}).items())
    command = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
    if headers:
        command += ["-headers", headers]
    return command + ["-i", stream["url"], "-vn", "-c:a", "copy", "-f", container, str(target)]


def grab_live(info: dict, out_dir: Path, stop: threading.Event) -> Path:
    stream = stream_of(info)
    if not stream.get("url"):
        raise ToolError("拿不到直播的串流網址")
    codec = (stream.get("acodec") or "").split(".")[0].lower()
    suffix, container = _CONTAINERS.get(codec, ("mka", "matroska"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_name(info.get('uploader') or info.get('title') or 'live')} 直播 {time.strftime('%Y%m%d-%H%M%S')}"
    target = unique_path(out_dir, stem, f".{suffix}")
    process = subprocess.Popen(live_command(stream, target, container), stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    # Drain ffmpeg's errors as they come: a full stderr pipe (network hiccups on a long
    # stream) would block ffmpeg, stall the file and make it deaf to "q".
    errors: collections.deque = collections.deque(maxlen=20)
    drain = threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True)
    drain.start()
    started = time.monotonic()
    while process.poll() is None:
        if stop.wait(1.0):
            try:
                process.stdin.write(b"q")       # ffmpeg finishes the file and exits
                process.stdin.flush()
            except OSError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
            break
        size = target.stat().st_size if target.exists() else 0
        report("live", seconds=round(time.monotonic() - started), bytes=size)
    drain.join(timeout=2)
    if not target.exists() or target.stat().st_size == 0:
        last = next((e.decode("utf-8", "replace").strip() for e in reversed(errors) if e.strip()), "")
        target.unlink(missing_ok=True)
        raise ToolError("直播錄音失敗" + (f"：{last[:160]}" if last else ""))
    return target


def grab_url(url: str, out_dir: Path, stop: threading.Event) -> Path:
    print("讀取網址資訊…", flush=True)
    info = fetch_info(url)
    if stop.is_set():
        raise ToolError("已停止，沒有存檔")
    live = is_live(info)
    print(f"{'直播' if live else '影片'}：{info.get('title') or '?'}", flush=True)
    if live:
        print("直播錄音中，按「停止」或等直播結束就會存檔", flush=True)
        return grab_live(info, out_dir, stop)
    return grab_vod(url, info, out_dir, stop)


# ---- what the computer plays ---------------------------------------------------------------
def _soundcard():
    import warnings

    import soundcard

    # "data discontinuity": Windows pauses a speaker's stream during silence and
    # soundcard fills those gaps itself, so the warning is only noise
    warnings.simplefilter("ignore", soundcard.SoundcardRuntimeWarning)
    return soundcard


def devices() -> list[dict]:
    """Speakers/headphones (record what you hear) first, then inputs such as CABLE Output."""
    sc = _soundcard()
    default = sc.default_speaker().id
    found = [{"name": s.name, "kind": "speaker", "default": s.id == default} for s in sc.all_speakers()]
    found.sort(key=lambda d: not d["default"])
    found += [{"name": m.name, "kind": "input", "default": False}
              for m in sc.all_microphones(include_loopback=False)]
    return found


def pick_device(name: str | None, kind: str | None = None):
    """Exact name first, then part of a name; kind ("speaker"/"input") settles devices that
    share a name, such as a Bluetooth headset's speaker and microphone."""
    sc = _soundcard()
    if not name:
        speaker = sc.default_speaker()
        return speaker.name, sc.get_microphone(speaker.id, include_loopback=True)
    candidates = []
    if kind in (None, "speaker"):
        candidates += [(s.name, lambda s=s: sc.get_microphone(s.id, include_loopback=True))
                       for s in sc.all_speakers()]
    if kind in (None, "input"):
        candidates += [(m.name, lambda m=m: m) for m in sc.all_microphones(include_loopback=False)]
    wanted = name.lower()
    for match in (lambda n: n.lower() == wanted, lambda n: wanted in n.lower()):
        for device_name, open_source in candidates:
            if match(device_name):
                return device_name, open_source()
    raise ToolError(f"找不到裝置「{name}」（用 devices 看可以錄哪些）")


def record(device_name: str | None, out_dir: Path, stop: threading.Event, kind: str | None = None) -> Path:
    import numpy as np
    import soundfile

    name, source = pick_device(device_name, kind)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(out_dir, f"錄音 {time.strftime('%Y%m%d-%H%M%S')}", ".flac")
    print(f"錄音中：{name}", flush=True)
    last_report, peak, frames = 0.0, 0.0, 0
    with soundfile.SoundFile(target, "w", SAMPLE_RATE, CHANNELS, subtype="PCM_24", format="FLAC") as out, \
            source.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=1024) as recorder:
        while not stop.is_set():
            # Blocks for 0.1 s of sound. Windows sends a speaker's stream only while
            # something plays; soundcard (>= 0.4.6) fills that silence with zeros from
            # its own clock, so the file keeps real time. Do not poll record(None) with
            # sleeps instead: it returns one ~10 ms packet per call and drops the rest.
            block = recorder.record(numframes=SAMPLE_RATE // 10)
            out.write(np.clip(block, -1.0, 1.0))
            frames += len(block)
            peak = max(peak, float(np.abs(block).max()) if len(block) else 0.0)
            now = time.monotonic()
            if now - last_report >= 1.0:
                report("record", seconds=round(frames / SAMPLE_RATE), level=round(peak, 3))
                last_report, peak = now, 0.0
    if frames == 0:
        target.unlink(missing_ok=True)
        raise ToolError("沒有錄到任何聲音")
    return target


# ---- command line --------------------------------------------------------------------------
def _watch_stop_file(path: Path, stop: threading.Event) -> None:
    # A file, not stdin: a thread blocked reading stdin froze soundcard's recorder
    # (no audio arrived at all while it waited), so the window signals with a file.
    while not stop.wait(0.2):
        if path.exists():
            stop.set()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="audio only: from a URL, or what the computer plays")
    sub = parser.add_subparsers(dest="mode", required=True)
    url_parser = sub.add_parser("url", help="the audio of a video or live stream")
    url_parser.add_argument("url")
    record_parser = sub.add_parser("record", help="what a speaker plays, or an input device, as FLAC")
    record_parser.add_argument("--device", help="device name, or part of it (default: your default speaker)")
    record_parser.add_argument("--kind", choices=("speaker", "input"),
                               help="only match speakers (what they play) or inputs")
    sub.add_parser("devices", help="list recordable devices as JSON")
    for p in (url_parser, record_parser):
        p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"default {DEFAULT_OUT}")
        p.add_argument("--stop-file", type=Path, help="stop (and save) once this file exists")
    args = parser.parse_args(argv)
    if args.mode == "devices":
        print(json.dumps(devices(), ensure_ascii=False), flush=True)
        return 0
    stop = threading.Event()
    if args.stop_file:
        threading.Thread(target=_watch_stop_file, args=(args.stop_file, stop), daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        # Ctrl+C in a terminal stops like the window's button: the file is still saved
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        if args.mode == "url":
            path = grab_url(args.url, args.out, stop)
        else:
            path = record(args.device, args.out, stop, args.kind)
    except ToolError as exc:
        print(f"[失敗] {exc}", file=sys.stderr)
        return 1
    print(f"完成：{path}", flush=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
