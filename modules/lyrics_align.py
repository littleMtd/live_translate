"""Lyric lines the owner supplies, timed against the audio by forced alignment.

lyric_lines() finds a lyric block in a video description so the GUI can pre-fill the
lyrics box for the owner to check; timed_lines() turns the aligner's words back into
one cue per lyric line.
"""
from __future__ import annotations

import re
import unicodedata

MIN_LINES = 8               # fewer Korean lines than this is not a lyric block
MIN_HANGUL = 60
MAX_LINE_CHARS = 80         # longer lines are prose, not lyrics

_HANGUL = re.compile(r"[가-힣]")
_KEEP = re.compile(r"[가-힣a-z0-9぀-ヿ一-鿿]")
_LINK = re.compile(r"https?://|www\.|@\w|#\S", re.I)
# "작사 : ...", "Lyrics by ...", "Vocal - ...": a short label then a separator
_CREDIT = re.compile(r"^[^:：\-|]{1,20}\s*[:：|]|^[^\-]{1,20}\s+-\s+|\bby\b|©|ⓒ|\(c\)", re.I)
_SECTION = re.compile(r"^[\[(（【].{0,20}[\])）】]$")
# Announcement sentences ("...나왔습니다", "...들어주세요"), not lyrics
_PROSE = re.compile(r"(니다|세요)[.!~♡♥\s]*$")


def _normal(text: str) -> str:
    # NFC: text pasted from macOS can carry decomposed Hangul, which _KEEP would drop
    return "".join(_KEEP.findall(unicodedata.normalize("NFC", text).lower()))


def lyric_lines(description: str) -> list[str]:
    """The description's lyric block (its longest run of lyric-like lines), or [] when it has none.

    A single credit-looking line between lyric lines ("Stand by me", "Baby: ...") stays in the
    block; two in a row are a credit list and end it."""
    runs, current, held = [], [], []
    for raw in (description or "").splitlines():
        line = raw.strip()
        if not line or _SECTION.match(line):
            continue                      # blank lines and [Chorus] headers do not end a block
        if (len(line) > MAX_LINE_CHARS or _LINK.search(line) or _PROSE.search(line)
                or not _normal(line) or (_CREDIT.search(line) and (held or not current))):
            if current:
                runs.append(current)
            current, held = [], []
        elif _CREDIT.search(line):
            held.append(line)             # kept only if another lyric line follows
        else:
            current += held + [line]
            held = []
    if current:
        runs.append(current)
    best = max(runs, key=lambda run: sum(len(_HANGUL.findall(line)) for line in run), default=[])
    korean = [line for line in best if _HANGUL.search(line)]
    if len(korean) < MIN_LINES or sum(len(_HANGUL.findall(line)) for line in best) < MIN_HANGUL:
        return []
    return best


def clean_lines(text: str) -> list[str]:
    """The owner's lyrics: one cue per non-empty line, [Chorus]-style headers dropped."""
    lines = [line.strip() for line in unicodedata.normalize("NFC", text or "").splitlines()]
    return [line for line in lines if line and not _SECTION.match(line) and _normal(line)]


def timed_lines(lines, words, duration):
    """Cues (start, end, line) from the aligner's words, which follow the text in order.

    words are (start, end, text), start/end None when the aligner gave no usable timing.
    The words' normalised characters must spell the lyrics; each line takes the next
    len(line) characters, so a word the aligner merged across two lines times both. A line
    with no timed character is skipped rather than guessed."""
    stream = []
    for start, end, text in words:
        chars = _normal(text)
        for i, char in enumerate(chars):
            # a word's time is shared evenly by its characters, so a merged word splits fairly
            if start is None:
                stream.append((char, None, None))
            else:
                step = (end - start) / len(chars)
                stream.append((char, start + i * step, start + (i + 1) * step))
    if "".join(c[0] for c in stream) != "".join(_normal(line) for line in lines):
        raise ValueError("aligner words do not spell the lyrics")
    cues, k = [], 0
    for line in lines:
        size = len(_normal(line))
        timed = [c for c in stream[k:k + size] if c[1] is not None]
        k += size
        if timed and timed[-1][2] > timed[0][1]:
            cues.append((timed[0][1], timed[-1][2], line))
    out = []
    for n, (start, end, line) in enumerate(cues):
        if n + 1 < len(cues):
            end = min(end, cues[n + 1][0])               # no two lines on screen at once
        if duration > 0:
            start, end = min(start, max(0.0, duration - 0.05)), min(end, duration)
        out.append((start, max(end, start + 0.05), line))
    return out
