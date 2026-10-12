"""Bookmarks for memorable live moments.

The subtitle window marks a moment; once the configured follow-up time has
passed (or the window closes) the subtitles shown just before and just after
the mark are appended to logs/bookmarks_YYYYMMDD.md. Only subtitle text is
written; nothing is sent anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from modules.live_status import SubtitleRecord

_DIR = Path(__file__).parent.parent / "logs"


@dataclass(frozen=True)
class Bookmark:
    marked_at: float        # monotonic clock of the mark (same clock as SubtitleRecord.at)
    wall_time: datetime
    elapsed_sec: float      # since the subtitle window opened
    profile_id: str
    before: tuple[SubtitleRecord, ...] = ()  # captured at the mark, so later lines cannot evict them
    after_sec: float = 20.0                  # follow-up window recorded after the mark


def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _lines(records: list[SubtitleRecord]) -> list[str]:
    out = []
    for record in records:
        if record.source:
            out.append(f"- {record.source}")
            out.append(f"  → {record.target}")
        else:
            out.append(f"- {record.target}")
    return out or ["- （沒有字幕）"]


def format_bookmark(mark: Bookmark, records: list[SubtitleRecord]) -> str:
    before = list(mark.before)
    after = [r for r in records if mark.marked_at < r.at <= mark.marked_at + mark.after_sec]
    header = f"## {mark.wall_time:%Y-%m-%d %H:%M:%S}（開啟後 {_elapsed(mark.elapsed_sec)}）"
    if mark.profile_id:
        header += f" · {mark.profile_id}"
    return "\n".join([header, "", "標記前：", *_lines(before), "", "標記後：", *_lines(after), "", ""])


def write_text(mark: Bookmark, text: str, directory: Path | None = None) -> Path:
    """Append an already formatted bookmark, so a retry writes exactly the same content."""
    directory = directory or _DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"bookmarks_{mark.wall_time:%Y%m%d}.md"
    # One write call per bookmark; unencodable text (lone surrogates) becomes "?".
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
    return path


def write_bookmark(mark: Bookmark, records: list[SubtitleRecord], directory: Path | None = None) -> Path:
    return write_text(mark, format_bookmark(mark, records), directory)
