"""
Download audio from a YouTube URL and save as WAV.

Usage:
    python scripts/download_audio.py https://youtu.be/LGcLBC9_RUk
    python scripts/download_audio.py https://youtu.be/LGcLBC9_RUk --out audio/my_file.wav
"""
import argparse
import subprocess
import sys
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument("--out", default=None, help="Output WAV path (default: audio/<video_id>.wav)")
    args = parser.parse_args()

    # Extract video ID for default filename
    video_id = args.url.split("v=")[-1].split("&")[0].split("/")[-1]
    out_path  = args.out or f"audio/{video_id}.wav"

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-x",
        "--audio-format", "wav",
        "--audio-quality", "0",
        "-o", out_path,
        args.url,
    ]

    print(f"Downloading: {args.url}")
    print(f"Output:      {out_path}\n")

    result = subprocess.run(cmd)
    if result.returncode == 0:
        size_mb = Path(out_path).stat().st_size / 1024 / 1024
        print(f"\nDone: {out_path}  ({size_mb:.1f} MB)")
    else:
        print("\nFailed. Make sure yt-dlp is installed: pip install yt-dlp")
        sys.exit(1)


if __name__ == "__main__":
    main()
