from __future__ import annotations

import re
from collections.abc import Mapping

from utils.logger import get_logger
from utils.text_heuristics import (
    DIGIT_RE,
    ENGLISH_WORD_RE,
    KOREAN_CHAR_RE,
    STT_FRAGMENTED_MARKERS,
    STT_GARBAGE_STRONG_KEYWORDS,
    STT_INSIGNIFICANT_RE,
    STT_ALLOWED_SHORT_ENGLISH_WORDS,
    STT_LOW_VALUE_CONTEXT_MARKERS,
    STT_LOW_VALUE_FRAGMENT_MARKERS,
    STT_SONG_CONTEXT_MARKERS,
    STT_SONG_FRAGMENT_MARKERS,
    STT_TEMPLATE_CONDITIONAL_PHRASES,
    STT_TEMPLATE_HARD_PREFIX_STRIP_PHRASES,
    STT_TEMPLATE_HARD_PHRASES,
    STT_TEMPLATE_STRIP_PHRASES,
)

log = get_logger("translation_policy")


def _word_counts(words: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    return counts


def _has_strong_garbage_keyword(text: str) -> bool:
    for keyword in STT_GARBAGE_STRONG_KEYWORDS:
        if keyword == "클릭":
            for match in re.finditer(re.escape(keyword), text):
                before = text[match.start() - 1] if match.start() > 0 else ""
                after = text[match.end()] if match.end() < len(text) else ""
                if KOREAN_CHAR_RE.match(before) or KOREAN_CHAR_RE.match(after):
                    continue
                return True
            continue

        if keyword != "광고":
            if keyword in text:
                return True
            continue

        for match in re.finditer(re.escape(keyword), text):
            if text[match.end():].startswith("용"):
                continue
            return True
    return False


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
        self._last_sanitize_rejection = ""

    def prepare_input(self, text: str) -> str | None:
        text = text.strip()
        self._last_sanitize_rejection = ""
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

        if reason in {"stt_garbage", "stt_template_garbage"}:
            sanitized = self.strip_stt_template_fragments(text)
            if sanitized and sanitized != text and self.rejection_reason(sanitized) is None:
                log.debug("Rescued STT template-tailed input: %.40s -> %.40s", text, sanitized)
                self.last_input = sanitized
                return sanitized

        # `stt_template_garbage` runs before `last_input` is updated for the same
        # reason as `too_long`: a fabricated outro/caption template must not poison
        # the duplicate slot. NOTE: this intentionally does NOT change whether the
        # existing `stt_garbage` path updates `last_input` (it still does, below).
        if reason == "stt_template_garbage":
            log.debug("Filtering STT template hallucination: %.40s", text)
            return None

        if reason == "stt_song_fragment":
            log.debug("Filtering STT song/lyrics fragment: %.40s", text)
            return None

        if reason == "stt_low_value_fragment":
            sanitized = self.strip_stt_low_value_fragments(text)
            if sanitized and sanitized != text and self.rejection_reason(sanitized) is None:
                log.debug("Rescued low-value STT fragment tail: %.40s -> %.40s", text, sanitized)
                self.last_input = sanitized
                return sanitized
            log.debug("Filtering low-value STT fragment: %.40s", text)
            return None

        self.last_input = text

        if reason == "too_short":
            log.debug("Skipping: too short (%d chars)", len(text))
            return None

        if reason == "stt_garbage":
            log.debug("Filtering STT garbage: %.40s", text)
            return None

        sanitized = self.strip_stt_template_fragments(text)
        if sanitized is None:
            self._last_sanitize_rejection = "stt_sanitized_empty"
            log.debug("Filtering STT template after stripping left no usable text: %.40s", text)
            return None

        if sanitized != text:
            log.debug("Sanitized STT template fragment: %.40s -> %.40s", text, sanitized)
            self.last_input = sanitized

        return sanitized

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
        if self.is_stt_song_fragment(text):
            return "stt_song_fragment"
        if self.is_stt_low_value_fragment(text):
            return "stt_low_value_fragment"
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

        word_count = _word_counts(words)

        repeat_ratio = max(word_count.values()) / len(words) if words else 0
        if repeat_ratio > 0.6:
            log.debug(
                "STT garbage detected: excessive repetition (ratio=%.2f) in '%s'",
                repeat_ratio,
                text[:50],
            )
            return True

        if _has_strong_garbage_keyword(text) and '?' not in text and '!' not in text:
            log.debug("STT garbage detected: commercial keywords in '%s'", text[:50])
            return True

        has_korean = bool(KOREAN_CHAR_RE.search(text))
        has_english = bool(ENGLISH_WORD_RE.search(text))

        if has_korean and has_english:
            english_words = [
                word
                for word in ENGLISH_WORD_RE.findall(text)
                if word.upper() not in STT_ALLOWED_SHORT_ENGLISH_WORDS
            ]
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

    @staticmethod
    def is_stt_song_fragment(text: str) -> bool:
        normalized = " ".join(text.split())
        if not normalized:
            return False

        words = normalized.split()
        if len(words) < 4:
            return False

        marker_hits = sum(normalized.count(marker) for marker in STT_SONG_FRAGMENT_MARKERS)
        has_song_context = any(marker in normalized for marker in STT_SONG_CONTEXT_MARKERS)
        repeated_short_words = sum(
            count
            for word, count in _word_counts(words).items()
            if count >= 2 and len(STT_INSIGNIFICANT_RE.sub("", word)) <= 3
        )
        punctuation_count = sum(normalized.count(mark) for mark in ("~", "…", "..."))

        if marker_hits >= 2 and has_song_context:
            log.debug("STT song fragment detected: markers=%d text='%s'", marker_hits, normalized[:50])
            return True

        if has_song_context and repeated_short_words >= 5 and punctuation_count >= 1:
            log.debug(
                "STT song fragment detected: repeated_short=%d text='%s'",
                repeated_short_words,
                normalized[:50],
            )
            return True

        return False

    @staticmethod
    def is_stt_low_value_fragment(text: str) -> bool:
        return TranslationPolicy.strip_stt_low_value_fragments(text) != " ".join(text.split())

    @staticmethod
    def strip_stt_template_fragments(text: str) -> str | None:
        normalized = " ".join(text.split())
        if not normalized:
            return None

        def _trim_leading_boundary(value: str) -> str:
            return value.lstrip(" \t\r\n.!?~…。．！？，、")

        def _trim_trailing_boundary(value: str) -> str:
            return value.rstrip(" \t\r\n.!?~…。．！？，、")

        def _has_useful_korean(value: str) -> bool:
            significant = STT_INSIGNIFICANT_RE.sub("", value)
            return len(significant) >= 2 and bool(KOREAN_CHAR_RE.search(value))

        def _strip_internal_template_phrases(value: str) -> tuple[str, bool]:
            changed_inner = False
            phrases = tuple(
                phrase
                for phrase in STT_TEMPLATE_STRIP_PHRASES
                if phrase != "구독과 좋아요는"
            )

            for phrase in sorted(set(phrases), key=len, reverse=True):
                search_start = 0
                while True:
                    start = value.find(phrase, search_start)
                    if start < 0:
                        break

                    end = start + len(phrase)
                    prefix = value[:start].rstrip()
                    suffix = _trim_leading_boundary(value[end:])
                    if not prefix or not suffix:
                        search_start = end
                        continue

                    if not _has_useful_korean(prefix) or not _has_useful_korean(suffix):
                        search_start = end
                        continue

                    value = f"{prefix} {suffix}".strip()
                    changed_inner = True
                    search_start = max(len(prefix), 0)

            return value, changed_inner

        stripped = normalized
        changed = False

        hard_stripped = TranslationPolicy._strip_trailing_hard_template_suffix(stripped)
        if hard_stripped != stripped:
            if hard_stripped is None:
                return None
            stripped = hard_stripped
            changed = True

        while True:
            next_value = stripped
            for phrase in STT_TEMPLATE_STRIP_PHRASES:
                if next_value.startswith(phrase):
                    next_value = _trim_leading_boundary(next_value[len(phrase):])
                    break
            if next_value == stripped:
                break
            stripped = next_value
            changed = True

        while True:
            trimmed = _trim_trailing_boundary(stripped)
            next_value = stripped
            for phrase in STT_TEMPLATE_STRIP_PHRASES:
                if trimmed.endswith(phrase):
                    next_value = trimmed[: -len(phrase)].rstrip()
                    break
            if next_value == stripped:
                break
            stripped = next_value
            changed = True

        stripped, internal_changed = _strip_internal_template_phrases(stripped)
        changed = changed or internal_changed

        if not changed:
            return normalized

        stripped = stripped.strip()
        significant = STT_INSIGNIFICANT_RE.sub("", stripped)
        if len(significant) < 2 or not KOREAN_CHAR_RE.search(stripped):
            return None

        return stripped

    @staticmethod
    def strip_stt_low_value_fragments(text: str) -> str | None:
        normalized = " ".join(text.split())
        if not normalized:
            return None

        marker_indexes = [
            index
            for marker in STT_LOW_VALUE_FRAGMENT_MARKERS
            if (index := normalized.find(marker)) >= 0
        ]
        if not marker_indexes:
            return normalized

        distinct_marker_count = len(marker_indexes)
        has_context = any(marker in normalized for marker in STT_LOW_VALUE_CONTEXT_MARKERS)
        starts_with_marker = min(marker_indexes) <= 1
        if distinct_marker_count < 2 and not (starts_with_marker or has_context):
            return normalized

        start = min(marker_indexes)
        prefix = normalized[:start].rstrip(" \t\r\n.!?~…。．！？，、")
        significant = STT_INSIGNIFICANT_RE.sub("", prefix)
        if len(significant) >= 6 and KOREAN_CHAR_RE.search(prefix):
            return prefix
        return None

    @staticmethod
    def _strip_trailing_hard_template_suffix(text: str) -> str | None:
        hard_phrases = (
            "한글 자막 제공",
            *STT_TEMPLATE_HARD_PREFIX_STRIP_PHRASES,
            *STT_TEMPLATE_HARD_PHRASES,
        )
        hard_indexes = [
            index
            for phrase in hard_phrases
            if (index := text.find(phrase)) >= 0
        ]
        if not hard_indexes:
            return text

        start = min(hard_indexes)
        prefix = text[:start].rstrip(" \t\r\n.!?~…。．！？，、")
        suffix = text[start:]
        residue = suffix
        for phrase in hard_phrases:
            residue = residue.replace(phrase, " ")
        residue = re.sub(r"[\s.,!?~…。．！？，、]+", "", residue)
        residue = residue.replace("및", "")
        residue = residue.replace("은", "")
        residue = residue.replace("는", "")
        residue = residue.replace("의", "")
        residue = residue.replace("를", "")
        residue = residue.replace("을", "")
        residue = residue.replace("이", "")
        residue = residue.replace("가", "")
        residue = residue.replace("로", "")
        residue = residue.replace("에서", "")

        if residue:
            return text
        if not prefix:
            return None
        return prefix
