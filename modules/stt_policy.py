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


def is_hallucinated(text: str, max_japanese_chars: int, logger=log) -> bool:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return True

    japanese = sum(1 for char in chars if "\u3040" <= char <= "\u309f" or "\u30a0" <= char <= "\u30ff")
    if japanese > max_japanese_chars:
        logger.debug("STT rejected (Japanese kana=%d): %s", japanese, text[:40])
        return True

    words = text.split()
    if len(words) >= 6:
        half = words[:len(words) // 2]
        if " ".join(half) in text[len(" ".join(half)):]:
            logger.debug("STT rejected (repetition loop): %s", text[:40])
            return True

    return False


def should_reject_language(detected_lang: str | None, text: str, logger=log) -> bool:
    if not detected_lang:
        return False

    lang_lower = detected_lang.lower()
    if lang_lower in ("ja", "japanese"):
        logger.warning("Groq STT rejected (lang=%s): %s", detected_lang, text[:40])
        return True

    if lang_lower not in ("ko", "korean"):
        logger.warning("Groq STT unexpected lang=%s (passing through): %s", detected_lang, text[:40])

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
    stats = segment_stats(segments)
    if stats is None:
        return False

    logger.debug(
        "Groq segment stats: no_speech=%.2f logprob=%.2f comp=%.2f",
        stats.no_speech,
        stats.logprob,
        stats.compression_ratio,
    )
    if stats.no_speech > no_speech_threshold:
        logger.warning("Groq STT rejected (no_speech_prob=%.2f): %s", stats.no_speech, text[:40])
        return True
    if stats.logprob < avg_logprob_threshold:
        logger.warning("Groq STT rejected (avg_logprob=%.2f): %s", stats.logprob, text[:40])
        return True
    if stats.compression_ratio > max_compression_ratio:
        logger.warning("Groq STT rejected (compression_ratio=%.2f): %s", stats.compression_ratio, text[:40])
        return True

    return False


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
    prompt_parts: list[str] = []

    normalized_seed = normalize_prompt_text(seed_prompt)
    if normalized_seed:
        prompt_parts.append(normalized_seed)

    def remaining_chars() -> int | None:
        if max_prompt_chars is None:
            return None
        used = _encoded_len("\n".join(prompt_parts))
        separator = 1 if prompt_parts else 0
        return max_prompt_chars - used - separator

    def append_with_budget(text: str) -> None:
        text = normalize_prompt_text(text)
        if not text:
            return
        budget = remaining_chars()
        if budget is not None:
            if budget <= 0:
                return
            if _encoded_len(text) > budget:
                text = _truncate_encoded(text, budget).rstrip(" ,.;")
        if text:
            prompt_parts.append(text)

    if use_profile_glossary:
        append_with_budget(glossary_builder(active_profile))

    recent_context = normalize_prompt_text(last_transcript, max_context_chars)
    if recent_context:
        append_with_budget(f"{_RECENT_CONTEXT_PREFIX}{recent_context}")

    prompt = "\n".join(prompt_parts)
    if max_prompt_chars is not None and _encoded_len(prompt) > max_prompt_chars:
        prompt = _truncate_encoded(prompt, max_prompt_chars)
    return prompt or None
