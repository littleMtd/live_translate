"""Bounded, metadata-only activity context shared by STT and translation."""

from __future__ import annotations

import re
import unicodedata


MAX_ACTIVITY_CHARS = 80
_UNSAFE_INSTRUCTION_RE = re.compile(
    r"(?:"
    r"\b(?:ignore|disregard|forget|override|follow|obey|reveal|show|print|repeat|translate)\b"
    r".{0,48}\b(?:instruction|prompt|message|rule)s?\b"
    r"|"
    r"\b(?:ignore|disregard|forget|override|follow|obey|reveal|show|print|repeat|translate)\b"
    r".{0,48}\b(?:system|developer|assistant|text)\b"
    r"|"
    r"\b(?:system|developer|assistant)\s*:"
    r")",
    re.IGNORECASE,
)


def normalize_activity(value: object, *, max_chars: int = MAX_ACTIVITY_CHARS) -> str:
    """Return one short line suitable for matching, logs, and prompt metadata."""
    if not isinstance(value, str):
        return ""
    # Reject controls, invisible format characters, surrogates, private-use,
    # and other Unicode "Other" categories before whitespace folding. This is
    # fail-closed metadata, not source speech, so preserving such characters
    # has no legitimate benefit and can hide prompt-like instructions.
    if any(unicodedata.category(char).startswith("C") for char in value):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return ""
    one_line = " ".join(normalized.split())
    if not one_line or max_chars <= 0:
        return ""
    if _UNSAFE_INSTRUCTION_RE.search(one_line):
        return ""
    return one_line[:max_chars].rstrip()


def activity_prompt_capsule(value: object) -> str:
    """Build the sole activity capsule; the value is context, never source."""
    activity = normalize_activity(value)
    if not activity:
        return ""
    return (
        f"[Background] Current stream activity: {activity}\n"
        "Use this metadata only to disambiguate game/context-specific terms. "
        "Never translate, mention, or copy it into the output."
    )
