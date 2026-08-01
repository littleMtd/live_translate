"""Record-only heuristics for evaluating a future sentence hold.

This module classifies likely unfinished Korean tails and evaluates the first
subsequent STT chunk. It does not sleep, buffer, merge, or otherwise alter the
live sentence path.
"""
from __future__ import annotations

from dataclasses import dataclass

from utils.text_heuristics import (
    SENTENCE_COMPLETE_ENDINGS,
    SENTENCE_INCOMPLETE_ENDINGS,
    STT_INSIGNIFICANT_RE,
)


_PARTICLE_ENDINGS = tuple(
    sorted(
        {
            "에게서",
            "한테서",
            "으로",
            "에서",
            "부터",
            "까지",
            "에게",
            "한테",
            "처럼",
            "보다",
            "로",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "에",
            "도",
            "만",
            "의",
            "와",
            "과",
        },
        key=len,
        reverse=True,
    )
)
_CONNECTOR_ENDINGS = tuple(
    ending
    for ending in SENTENCE_INCOMPLETE_ENDINGS
    if ending not in _PARTICLE_ENDINGS
)
_ADNOMINAL_ENDINGS = ("던",)
_PAIRED_DELIMITERS = (
    ("(", ")"),
    ("[", "]"),
    ("{", "}"),
    ("<", ">"),
    ("（", "）"),
    ("【", "】"),
    ("《", "》"),
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ("‘", "’"),
)
# A lone ASCII apostrophe is overwhelmingly an English contraction/possessive
# in live STT ("can't", "I'm"), not an opening quote. Track only double quote;
# curly single quotes remain safely paired above.
_SYMMETRIC_QUOTES = ('"',)
_MIN_GRAMMATICAL_TAIL_TEXT = 4
_MIN_LEXICAL_TAIL_TEXT = 3


def _matched_ending(text: str, endings: tuple[str, ...]) -> str:
    return next((ending for ending in endings if text.endswith(ending)), "")


def _unclosed_delimiters(text: str) -> tuple[str, ...]:
    unclosed = [
        opening
        for opening, closing in _PAIRED_DELIMITERS
        if text.count(opening) > text.count(closing)
    ]
    unclosed.extend(quote for quote in _SYMMETRIC_QUOTES if text.count(quote) % 2)
    return tuple(unclosed)


def _is_complete(text: str) -> bool:
    stripped = text.rstrip()
    if _matched_ending(stripped, SENTENCE_INCOMPLETE_ENDINGS):
        return False
    return bool(_matched_ending(stripped, SENTENCE_COMPLETE_ENDINGS))


@dataclass(frozen=True)
class UnfinishedTail:
    signals: tuple[str, ...]
    matched_ending: str = ""
    unclosed_delimiters: tuple[str, ...] = ()


def analyze_unfinished_tail(
    text: str,
    *,
    forced: bool,
    grammatical_min_significant: int = _MIN_GRAMMATICAL_TAIL_TEXT,
    include_adnominal: bool = False,
) -> UnfinishedTail:
    """Classify conservative unfinished-tail signals for shadow telemetry."""
    stripped = (text or "").rstrip()
    if not stripped:
        return UnfinishedTail(())

    signals: list[str] = []
    significant_len = len(STT_INSIGNIFICANT_RE.sub("", stripped))
    particle = (
        _matched_ending(stripped, _PARTICLE_ENDINGS)
        if significant_len >= grammatical_min_significant
        else ""
    )
    connector = (
        _matched_ending(stripped, _CONNECTOR_ENDINGS)
        if significant_len >= grammatical_min_significant
        else ""
    )
    adnominal = (
        _matched_ending(stripped, _ADNOMINAL_ENDINGS)
        if include_adnominal and significant_len >= grammatical_min_significant
        else ""
    )
    matched_ending = particle or connector or adnominal
    if particle:
        signals.append("unfinished_particle")
    elif connector:
        signals.append("unfinished_connector")
    elif adnominal:
        signals.append("unfinished_adnominal")

    unclosed = _unclosed_delimiters(stripped)
    if unclosed:
        signals.append("unclosed_delimiter")

    if (
        forced
        and significant_len >= _MIN_LEXICAL_TAIL_TEXT
        and not matched_ending
        and not _is_complete(stripped)
        and "가" <= stripped[-1] <= "힣"
    ):
        signals.append("possible_truncated_lexical_tail")

    return UnfinishedTail(tuple(signals), matched_ending, unclosed)


def evaluate_next_chunk(
    candidate_text: str,
    next_chunk_text: str,
    candidate: UnfinishedTail,
) -> dict[str, object]:
    """Estimate whether one observed next STT chunk would be a useful merge."""
    next_text = (next_chunk_text or "").strip()
    merged = f"{candidate_text.rstrip()} {next_text}".strip()
    merged_tail = analyze_unfinished_tail(merged, forced=False)
    meaningful_next = len(STT_INSIGNIFICANT_RE.sub("", next_text)) >= 2
    delimiter_resolved = bool(
        candidate.unclosed_delimiters
        and not set(candidate.unclosed_delimiters).intersection(
            merged_tail.unclosed_delimiters
        )
    )
    merged_complete = _is_complete(merged)
    raw_continuation = meaningful_next and bool(
        {"unfinished_connector", "unfinished_particle"}.intersection(
            candidate.signals
        )
    )
    # Text alone cannot prove that a following complete sentence belongs to a
    # connector/particle/lexical fragment. Keep that as weak/raw evidence.
    # The actionable gate uses only an observable structural repair.
    structural_resolution = meaningful_next and delimiter_resolved
    return {
        "next_chunk_text": next_text,
        "merged_text": merged,
        "merged_complete": merged_complete,
        "delimiter_resolved": delimiter_resolved,
        "raw_continuation_heuristic": raw_continuation,
        "structural_resolution": structural_resolution,
        "useful_merge_heuristic": structural_resolution,
        "remaining_signals": list(merged_tail.signals),
    }
