from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from utils.logger import get_logger

log = get_logger("stt_policy")

_RECENT_CONTEXT_PREFIX = "Recent Korean transcript context: "


@dataclass(frozen=True)
class SegmentStats:
    no_speech: float
    logprob: float
    compression_ratio: float


def normalize_prompt_text(text: str, max_chars: int | None = None) -> str:
    text = " ".join(text.split())
    if max_chars is not None and len(text) > max_chars:
        text = text[-max_chars:]
    return text


def _encoded_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_encoded(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if _encoded_len(text) <= max_chars:
        return text
    return text.encode("utf-8")[:max_chars].decode("utf-8", errors="ignore").rstrip()


def dedupe_transcript_overlap(
    previous: str,
    current: str,
    *,
    max_overlap_tokens: int = 12,
    min_overlap_tokens: int = 2,
    min_overlap_chars: int = 5,
) -> str:
    """Remove duplicated leading text caused by overlapped audio chunks."""
    previous = " ".join((previous or "").split())
    current = " ".join((current or "").split())
    if not previous or not current:
        return current

    prev_words = previous.split()
    cur_words = current.split()
    max_tokens = min(max_overlap_tokens, len(prev_words), len(cur_words))
    for count in range(max_tokens, min_overlap_tokens - 1, -1):
        if prev_words[-count:] == cur_words[:count]:
            overlap_text = " ".join(cur_words[:count])
            if len(overlap_text.replace(" ", "")) >= min_overlap_chars:
                return " ".join(cur_words[count:]).strip()

    max_chars = min(80, len(previous), len(current))
    for count in range(max_chars, min_overlap_chars - 1, -1):
        if previous[-count:] == current[:count]:
            return current[count:].lstrip()

    return current


def is_hallucinated(
    text: str,
    max_japanese_chars: int,
    logger=log,
    *,
    max_repeat_ratio: float = 0.7,
    allow_japanese: bool = False,
) -> bool:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return True

    japanese = sum(1 for char in chars if "\u3040" <= char <= "\u309f" or "\u30a0" <= char <= "\u30ff")
    if not allow_japanese and japanese > max_japanese_chars:
        logger.debug("STT rejected (Japanese kana=%d): %s", japanese, text[:40])
        return True

    words = text.split()
    try:
        repeat_ratio = float(max_repeat_ratio)
    except (TypeError, ValueError):
        repeat_ratio = 0.7
    repeat_ratio = min(1.0, max(0.0, repeat_ratio))
    # Four-word repetitions are common in natural livestream emphasis. Keep
    # the old six-word floor, then let max_repeat_ratio tune longer loops.
    if len(words) >= 6:
        for phrase_len in range(2, len(words) // 2 + 1):
            for start in range(0, len(words) - 2 * phrase_len + 1):
                phrase = words[start:start + phrase_len]
                for repeat_start in range(start + phrase_len, len(words) - phrase_len + 1):
                    if phrase != words[repeat_start:repeat_start + phrase_len]:
                        continue
                    if (phrase_len * 2) / len(words) >= repeat_ratio:
                        logger.debug("STT rejected (repetition loop): %s", text[:40])
                        return True

    return False


def should_reject_language(
    detected_lang: str | None,
    text: str,
    logger=log,
    *,
    allow_japanese: bool = False,
) -> bool:
    if not detected_lang:
        return False

    lang_lower = detected_lang.lower()
    if lang_lower in ("ja", "japanese"):
        if allow_japanese:
            logger.info("Groq STT accepted coherent foreign speech (lang=%s)", detected_lang)
            return False
        logger.warning("Groq STT rejected (lang=%s)", detected_lang)
        return True

    if lang_lower not in ("ko", "korean"):
        logger.warning("Groq STT unexpected lang=%s (passing through)", detected_lang)

    return False


def segment_stats(segments: list[dict]) -> SegmentStats | None:
    if not segments:
        return None

    return SegmentStats(
        no_speech=sum(segment.get("no_speech_prob", 0) for segment in segments) / len(segments),
        logprob=sum(segment.get("avg_logprob", 0) for segment in segments) / len(segments),
        compression_ratio=sum(segment.get("compression_ratio", 0) for segment in segments) / len(segments),
    )


def should_reject_segments(
    segments: list[dict],
    *,
    text: str,
    no_speech_threshold: float,
    avg_logprob_threshold: float,
    max_compression_ratio: float = 2.4,
    logger=log,
) -> bool:
    reason, _stats = segment_rejection_reason(
        segments,
        text=text,
        no_speech_threshold=no_speech_threshold,
        avg_logprob_threshold=avg_logprob_threshold,
        max_compression_ratio=max_compression_ratio,
        logger=logger,
    )
    return reason is not None


def segment_rejection_reason(
    segments: list[dict],
    *,
    text: str,
    no_speech_threshold: float,
    avg_logprob_threshold: float,
    max_compression_ratio: float = 2.4,
    logger=log,
) -> tuple[str | None, SegmentStats | None]:
    stats = segment_stats(segments)
    if stats is None:
        return None, None

    logger.debug(
        "Groq segment stats: no_speech=%.2f logprob=%.2f comp=%.2f",
        stats.no_speech,
        stats.logprob,
        stats.compression_ratio,
    )
    if stats.no_speech > no_speech_threshold:
        logger.warning("Groq STT rejected (no_speech_prob=%.2f)", stats.no_speech)
        return "no_speech_prob", stats
    if stats.logprob < avg_logprob_threshold:
        logger.warning("Groq STT rejected (avg_logprob=%.2f)", stats.logprob)
        return "avg_logprob", stats
    if stats.compression_ratio > max_compression_ratio:
        logger.warning("Groq STT rejected (compression_ratio=%.2f)", stats.compression_ratio)
        return "compression_ratio", stats

    return None, stats


@dataclass(frozen=True)
class GroqPromptBudget:
    """Prompt assembled for a Groq STT request plus how it fit the budget.

    Lets the runtime log answer "did a long glossary crowd out the recent
    context?" — the exact failure that blew the Groq prompt length before.
    """
    prompt: str | None
    prompt_bytes: int
    max_prompt_bytes: int | None
    glossary_present: bool       # a profile glossary was requested and non-empty
    glossary_truncated: bool     # it had to be shortened (or fully dropped) to fit
    context_present: bool        # there was a recent-transcript tail to add
    context_included: bool       # …and it actually survived into the final prompt


def build_groq_prompt_budget(
    *,
    seed_prompt: str,
    use_profile_glossary: bool,
    active_profile: str,
    last_transcript: str,
    glossary_builder: Callable[[str], str],
    max_context_chars: int,
    max_prompt_chars: int | None = None,
) -> GroqPromptBudget:
    prompt_parts: list[str] = []
    glossary_present = False
    glossary_truncated = False
    context_included = False

    normalized_seed = normalize_prompt_text(seed_prompt)
    if normalized_seed:
        prompt_parts.append(normalized_seed)

    def remaining_chars() -> int | None:
        if max_prompt_chars is None:
            return None
        used = _encoded_len("\n".join(prompt_parts))
        separator = 1 if prompt_parts else 0
        return max_prompt_chars - used - separator

    def append_with_budget(text: str, *, reserve_after: int = 0) -> tuple[bool, bool]:
        """Append `text` if it fits. Returns (appended, truncated)."""
        text = normalize_prompt_text(text)
        if not text:
            return False, False
        budget = remaining_chars()
        truncated = False
        if budget is not None:
            budget -= reserve_after
            if budget <= 0:
                return False, True
            if _encoded_len(text) > budget:
                text = _truncate_encoded(text, budget).rstrip(" ,.;")
                truncated = True
        if text:
            prompt_parts.append(text)
            return True, truncated
        return False, truncated

    recent_context = normalize_prompt_text(last_transcript, max_context_chars)
    recent_context_part = (
        f"{_RECENT_CONTEXT_PREFIX}{recent_context}" if recent_context else ""
    )
    context_present = bool(recent_context_part)

    if use_profile_glossary:
        glossary_text = normalize_prompt_text(glossary_builder(active_profile))
        glossary_present = bool(glossary_text)
        reserve_after = 0
        if recent_context_part and max_prompt_chars is not None:
            # Keep the previous transcript tail available even when a profile
            # glossary is long. The glossary can be truncated; context cannot
            # help STT if it is crowded out entirely.
            reserve_after = 1 + _encoded_len(recent_context_part)
        appended, truncated = append_with_budget(glossary_text, reserve_after=reserve_after)
        if glossary_present and (truncated or not appended):
            glossary_truncated = True

    if recent_context_part:
        context_included, _ = append_with_budget(recent_context_part)

    prompt = "\n".join(prompt_parts)
    if max_prompt_chars is not None and _encoded_len(prompt) > max_prompt_chars:
        prompt = _truncate_encoded(prompt, max_prompt_chars)
        context_included = _RECENT_CONTEXT_PREFIX in prompt

    prompt = prompt or None
    return GroqPromptBudget(
        prompt=prompt,
        prompt_bytes=_encoded_len(prompt) if prompt else 0,
        max_prompt_bytes=max_prompt_chars,
        glossary_present=glossary_present,
        glossary_truncated=glossary_truncated,
        context_present=context_present,
        context_included=context_included,
    )


def build_groq_prompt(
    *,
    seed_prompt: str,
    use_profile_glossary: bool,
    active_profile: str,
    last_transcript: str,
    glossary_builder: Callable[[str], str],
    max_context_chars: int,
    max_prompt_chars: int | None = None,
) -> str | None:
    """Backward-compatible wrapper returning just the assembled prompt string."""
    return build_groq_prompt_budget(
        seed_prompt=seed_prompt,
        use_profile_glossary=use_profile_glossary,
        active_profile=active_profile,
        last_transcript=last_transcript,
        glossary_builder=glossary_builder,
        max_context_chars=max_context_chars,
        max_prompt_chars=max_prompt_chars,
    ).prompt
