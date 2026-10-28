"""Official lyrics from a video description, timed by speech-recognition words.

Speech recognition mishears sung Korean; uploaders often put the exact lyrics in the
description. Each lyric line is located in the recognised text by fuzzy matching (a
chorus can match several times), the best non-overlapping set of matches becomes the
cues with the official wording, and words no line covers stay as recognised.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import replace

MIN_LINES = 8               # fewer Korean lines than this is not a lyric block
MIN_HANGUL = 60
MAX_LINE_CHARS = 80         # longer lines are prose, not lyrics
MIN_MATCH_CHARS = 4         # shorter lines ("아", "oh") would match anywhere
MAX_ERROR_RATE = 0.4        # edit distance allowed per lyric character
GAP_ERROR_RATE = 0.75       # looser, for a line whose neighbours on both sides matched
MIN_COVERAGE = 0.4          # below this share of recognised text, the lyrics are not this song's
MAX_WORK = 40_000_000       # DP cells; beyond this (an hours-long video) matching is skipped

_HANGUL = re.compile(r"[가-힣]")
_KEEP = re.compile(r"[가-힣a-z0-9]")
_LINK = re.compile(r"https?://|www\.|@\w|#\S", re.I)
# "작사 : ...", "Lyrics by ...", "Vocal - ...": a short label then a separator
_CREDIT = re.compile(r"^[^:：\-|]{1,20}\s*[:：|]|^[^\-]{1,20}\s+-\s+|\bby\b|©|ⓒ|\(c\)", re.I)
_SECTION = re.compile(r"^[\[(（【].{0,20}[\])）】]$")
# Announcement sentences ("...나왔습니다", "...들어주세요"), not lyrics
_PROSE = re.compile(r"(니다|세요)[.!~♡♥\s]*$")


def _normal(text: str) -> str:
    return "".join(_KEEP.findall(text.lower()))


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


def _matches(pattern: str, stream: str) -> list[tuple[int, int, int]]:
    """(start, end, errors) of pattern's approximate occurrences in stream (Sellers' algorithm)."""
    m, limit = len(pattern), int(len(pattern) * MAX_ERROR_RATE)
    cost = list(range(m + 1))
    origin = [0] * (m + 1)
    hits = []
    for j, char in enumerate(stream, 1):
        prev_cost, prev_origin = cost[0], origin[0]      # diagonal neighbours
        cost[0], origin[0] = 0, j                        # an occurrence may start anywhere
        for i in range(1, m + 1):
            diag = prev_cost + (pattern[i - 1] != char)
            prev_cost, prev_origin, here_origin = cost[i], origin[i], prev_origin
            best, start = diag, here_origin
            if cost[i] + 1 < best:                       # extra recognised character
                best, start = cost[i] + 1, origin[i]
            if cost[i - 1] + 1 < best:                   # lyric character not heard
                best, start = cost[i - 1] + 1, origin[i - 1]
            cost[i], origin[i] = best, start
        if cost[m] <= limit:
            hits.append((origin[m], j, cost[m]))
    # neighbouring end positions describe the same occurrence: keep the best of each
    kept = []
    for hit in sorted(hits, key=lambda h: (h[2], h[1] - h[0])):
        if all(hit[1] <= k[0] or hit[0] >= k[1] for k in kept):
            kept.append(hit)
    return kept


def _schedule(candidates):
    """The non-overlapping candidates with the most matched characters (weighted interval scheduling)."""
    ordered = sorted(candidates, key=lambda c: c[1])
    ends = [c[1] for c in ordered]
    best = [(0, ())]
    for k, cand in enumerate(ordered):
        before = bisect.bisect_right(ends, cand[0], 0, k)
        take = (best[before][0] + cand[3], best[before][1] + (k,))
        best.append(max(best[-1], take, key=lambda t: t[0]))
    return [ordered[k] for k in best[-1][1]]


def _distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1]


def _fill_gaps(chosen, patterns, stream):
    """A line missing between two matched neighbours is placed in the stretch between them when
    that stretch is about its length: heavy mishearing, not a missing line. Neighbours are
    compared by text, since a repeated chorus line may have matched as another copy."""
    text = dict(patterns)
    order = sorted(text)
    chosen = sorted(chosen)
    filled = []
    for a, b in zip(chosen, chosen[1:]):
        gap = stream[a[1]:b[0]]
        if not gap:
            continue
        for before, n, after in zip(order, order[1:], order[2:]):
            pattern = text[n]
            if (text[before] == text[a[2]] and text[after] == text[b[2]]
                    and 0.7 * len(pattern) <= len(gap) <= 1.4 * len(pattern)
                    and _distance(pattern, gap) <= len(pattern) * GAP_ERROR_RATE):
                filled.append((a[1], b[0], n, 0))
                break
    return sorted(chosen + filled)


def align(words, lines, duration, group):
    """(cues, stats). words: (start, end, text) sorted by time; group(words, duration) -> cues
    for the words no lyric line covers. cues is None when the lyrics do not fit this audio."""
    owner, stream = [], []
    for index, word in enumerate(words):
        for char in _normal(word[2]):
            owner.append(index)
            stream.append(char)
    stream = "".join(stream)
    patterns = [(n, _normal(line)) for n, line in enumerate(lines)]
    patterns = [(n, p) for n, p in patterns if len(p) >= MIN_MATCH_CHARS]
    stats = {"lyrics_lines": len(lines)}
    if not stream or not patterns or len(stream) * sum(len(p) for _, p in patterns) > MAX_WORK:
        return None, {**stats, "lyrics_used": False}
    candidates = [(start, end, n, len(p) - errors)
                  for n, p in patterns for start, end, errors in _matches(p, stream)]
    chosen = _fill_gaps(_schedule(candidates), patterns, stream)
    covered = sum(c[1] - c[0] for c in chosen)
    stats["lyrics_coverage"] = round(covered / len(stream), 3)
    if covered < MIN_COVERAGE * len(stream):
        return None, {**stats, "lyrics_used": False}

    # A word belongs to the line holding most of its characters.
    votes = {}
    for start, end, n, _ in chosen:
        for pos in range(start, end):
            votes.setdefault(owner[pos], {}).setdefault((start, n), 0)
            votes[owner[pos]][(start, n)] += 1
    line_words, runs, run = {}, [], []
    for index, word in enumerate(words):
        if index in votes:
            line_words.setdefault(max(votes[index], key=votes[index].get), []).append(word)
            if run:
                runs.append(run)
                run = []
        else:
            run.append(word)
    if run:
        runs.append(run)
    cues = [(ws[0][0], ws[-1][1], lines[n]) for (_, n), ws in line_words.items()]
    # each stretch between lyric lines is grouped on its own, so no cue spans a lyric line
    cues += [(c.start, c.end, c.text) for run in runs for c in group(run, duration)]
    cues.sort()
    out = []
    for k, (start, end, text) in enumerate(cues):
        if k + 1 < len(cues) and cues[k + 1][0] < end:
            end = max(cues[k + 1][0], start + 0.05)      # no two cues on screen at once
        if duration > 0:
            start = min(start, max(0.0, duration - 0.05))
            end = min(end, duration)
        out.append((start, max(end, start + 0.05), text))
    from modules.offline_subtitles import Cue
    stats.update(lyrics_used=True, lyrics_matched_lines=len({n for _, n in line_words}),
                 lyrics_cues=len(line_words), stt_cues=len(out) - len(line_words))
    return [replace(Cue("", s, e, t), id=f"c{i}") for i, (s, e, t) in enumerate(out, 1)], stats
