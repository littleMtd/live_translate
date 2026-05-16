from __future__ import annotations

from collections.abc import Mapping

from utils.logger import get_logger
from utils.text_heuristics import (
    DIGIT_RE,
    ENGLISH_WORD_RE,
    KOREAN_CHAR_RE,
    STT_FRAGMENTED_MARKERS,
    STT_GARBAGE_STRONG_KEYWORDS,
    STT_INSIGNIFICANT_RE,
    STT_TEMPLATE_CONDITIONAL_PHRASES,
    STT_TEMPLATE_HARD_PHRASES,
)

log = get_logger("translation_policy")


class TranslationPolicy:
    def __init__(
        self,
        *,
        slang: Mapping[str, str],
        min_translate_chars: int = 2,
        max_translate_chars: int = 500,
        last_input: str = "",
    ):
        self._slang = slang
        self._min_translate_chars = min_translate_chars
        self._max_translate_chars = max_translate_chars
        self.last_input = last_input

    def prepare_input(self, text: str) -> str | None:
        text = text.strip()
        reason = self.rejection_reason(text)
        if reason == "empty":
            return None

        if reason == "duplicate":
            log.debug("Duplicate input suppressed: %.40s", text)
            return None

        # `too_long` deliberately runs before `last_input` is updated: an oversized
        # STT hallucination must not poison the duplicate-suppression slot, otherwise
        # the next legitimate input that happens to match it would be silently dropped.
        if reason == "too_long":
            log.debug("Skipping: too long (%d chars)", len(text))
            return None

        # `stt_template_garbage` runs before `last_input` is updated for the same
        # reason as `too_long`: a fabricated outro/caption template must not poison
        # the duplicate slot. NOTE: this intentionally does NOT change whether the
        # existing `stt_garbage` path updates `last_input` (it still does, below).
        if reason == "stt_template_garbage":
            log.debug("Filtering STT template hallucination: %.40s", text)
            return None
        self.last_input = text

        if reason == "too_short":
            log.debug("Skipping: too short (%d chars)", len(text))
            return None

        if reason == "stt_garbage":
            log.debug("Filtering STT garbage: %.40s", text)
            return None

        return text

    def rejection_reason(self, text: str) -> str | None:
        text = text.strip()
        if not text:
            return "empty"
        if text == self.last_input:
            return "duplicate"
        if len(text) < self._min_translate_chars:
            return "too_short"
        if len(text) > self._max_translate_chars:
            return "too_long"
        if self.is_stt_garbage(text):
            return "stt_garbage"
        if self.is_stt_template_garbage(text):
            return "stt_template_garbage"
        return None

    def reset_last_input(self) -> None:
        self.last_input = ""

    def slang_result(self, text: str) -> str | None:
        return self._slang.get(text)

    @staticmethod
    def is_stt_garbage(text: str) -> bool:
        words = text.split()
        if len(words) < 3:
            return False

        word_count = {}
        for word in words:
            word_count[word] = word_count.get(word, 0) + 1

        repeat_ratio = max(word_count.values()) / len(words) if words else 0
        if repeat_ratio > 0.6:
            log.debug(
                "STT garbage detected: excessive repetition (ratio=%.2f) in '%s'",
                repeat_ratio,
                text[:50],
            )
            return True

        if (
            any(keyword in text for keyword in STT_GARBAGE_STRONG_KEYWORDS)
            and '?' not in text
            and '!' not in text
        ):
            log.debug("STT garbage detected: commercial keywords in '%s'", text[:50])
            return True

        has_korean = bool(KOREAN_CHAR_RE.search(text))
        has_english = bool(ENGLISH_WORD_RE.search(text))

        if has_korean and has_english:
            english_words = ENGLISH_WORD_RE.findall(text)
            if len(english_words) >= 3 and all(len(word) < 4 for word in english_words):
                log.debug("STT garbage detected: random english mixed with korean in '%s'", text[:50])
                return True

        digits = DIGIT_RE.findall(text)

        if len(digits) >= 2:
            unique_digits = set(digits)
            repeat_ratio = 1.0 - (len(unique_digits) / len(digits))
            fragmented_count = sum(1 for marker in STT_FRAGMENTED_MARKERS if marker in text)

            if repeat_ratio > 0.5 and fragmented_count >= 2 and len(text) < 50:
                log.debug(
                    "STT garbage detected: confused digits+fragmented in '%s' "
                    "(digit_repeat=%.1f%%, markers=%d)",
                    text[:50],
                    repeat_ratio * 100,
                    fragmented_count,
                )
                return True

        return False

    @staticmethod
    def is_stt_template_garbage(text: str) -> bool:
        """Detect STT-fabricated YouTube/boilerplate template hallucinations.

        Runs *after* `is_stt_garbage` (see `rejection_reason`), so existing
        commercial-keyword positives keep `filter_reason=stt_garbage`; this
        only catches templates that currently leak (e.g. trailing `!` defeats
        the `광고` keyword rule, or pure outro CTAs with no `광고` at all).
        """
        # Whitespace-normalized form for substring / phrase matching.
        normalized = " ".join(text.split())
        if not normalized:
            return False

        # HARD: any disclaimer phrase present → garbage, no extra condition.
        for phrase in STT_TEMPLATE_HARD_PHRASES:
            if phrase in normalized:
                log.debug("STT template (hard): %.50s", normalized)
                return True

        def _significant(value: str) -> str:
            return STT_INSIGNIFICANT_RE.sub("", value)

        total_significant = len(_significant(normalized))
        if total_significant == 0:
            return False

        remainder = normalized
        for phrase in STT_TEMPLATE_CONDITIONAL_PHRASES:
            count = remainder.count(phrase)
            if count == 0:
                continue
            # Rule 3 (independent): the same template repeated ≥ 2 times is
            # garbage regardless of ratio (e.g. "...감사합니다. ...감사합니다.").
            if count >= 2:
                log.debug("STT template (repeated x%d): %.50s", count, normalized)
                return True
            remainder = remainder.replace(phrase, " ")

        if remainder == normalized:
            return False  # no conditional phrase matched at all

        remaining_significant_chars = len(_significant(remainder))
        template_ratio = (total_significant - remaining_significant_chars) / total_significant

        # Rules 1 & 2 are ANDed (Codex §10.7/§10.9): only block when the
        # template both dominates the utterance AND leaves almost nothing else,
        # so real content + a template tail (high remainder) is NOT blocked.
        if remaining_significant_chars <= 4 and template_ratio >= 0.65:
            log.debug(
                "STT template (dominant: remain=%d ratio=%.2f): %.50s",
                remaining_significant_chars,
                template_ratio,
                normalized,
            )
            return True

        return False
