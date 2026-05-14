from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from utils.logger import get_logger

log = get_logger("stt_policy")


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
) -> str | None:
    prompt_parts: list[str] = []

    normalized_seed = normalize_prompt_text(seed_prompt)
    if normalized_seed:
        prompt_parts.append(normalized_seed)

    if use_profile_glossary:
        glossary_prompt = normalize_prompt_text(glossary_builder(active_profile))
        if glossary_prompt:
            prompt_parts.append(glossary_prompt)

    recent_context = normalize_prompt_text(last_transcript, max_context_chars)
    if recent_context:
        prompt_parts.append(f"Recent Korean transcript context: {recent_context}")

    return "\n".join(prompt_parts) or None
