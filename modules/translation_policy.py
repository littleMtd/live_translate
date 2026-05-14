from __future__ import annotations

from collections.abc import Mapping

from utils.logger import get_logger
from utils.text_heuristics import (
    DIGIT_RE,
    ENGLISH_WORD_RE,
    KOREAN_CHAR_RE,
    STT_FRAGMENTED_MARKERS,
    STT_GARBAGE_KEYWORDS,
)

log = get_logger("translation_policy")


class TranslationPolicy:
    def __init__(
        self,
        *,
        slang: Mapping[str, str],
        min_translate_chars: int = 2,
        last_input: str = "",
    ):
        self._slang = slang
        self._min_translate_chars = min_translate_chars
        self.last_input = last_input

    def prepare_input(self, text: str) -> str | None:
        text = text.strip()
        if not text:
            return None

        if text == self.last_input:
            log.debug("Duplicate input suppressed: %.40s", text)
            return None
        self.last_input = text

        if len(text) < self._min_translate_chars:
            log.debug("Skipping: too short (%d chars)", len(text))
            return None

        if self.is_stt_garbage(text):
            log.debug("Filtering STT garbage: %.40s", text)
            return None

        return text

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

        if any(keyword in text for keyword in STT_GARBAGE_KEYWORDS) and '?' not in text and '!' not in text:
            log.debug("STT garbage detected: commercial keywords in '%s'", text[:50])
            return True

        has_korean = bool(KOREAN_CHAR_RE.search(text))
        has_english = bool(ENGLISH_WORD_RE.search(text))

        if has_korean and has_english:
            english_words = ENGLISH_WORD_RE.findall(text)
            if all(len(word) < 4 for word in english_words):
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
