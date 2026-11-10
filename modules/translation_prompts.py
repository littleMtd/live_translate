import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from modules.streamer_profiles import canonical_profile_id


_PROFILE_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "translation_profiles.json"


def _string_profile_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    if not all(isinstance(key, str) and isinstance(text, str) for key, text in value.items()):
        raise ValueError(f"{field_name} must map strings to strings")
    return dict(value)


def _load_translation_profiles(path: Path = _PROFILE_DATA_PATH) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("translation profile data must be a JSON object")
    return _string_profile_map(data.get("profiles"), "profiles")


_STREAMER_PROFILES = _load_translation_profiles()


def get_translation_profile(profile_id: str) -> str:
    """The one hand-written profile: glossary, background notes and examples."""
    return _STREAMER_PROFILES.get(canonical_profile_id(profile_id), "")


def get_translation_profile_facts(profile_id: str) -> str:
    """Return the profile's glossary block for the compact fallback prompts.

    Profiles put exact mappings before the first blank line and notes and
    examples afterwards; the Groq and offline prompts take only the mappings.
    """
    profile = get_translation_profile(profile_id).strip()
    return profile.split("\n\n", 1)[0].strip() if profile else ""


@lru_cache(maxsize=16)
def get_translation_profile_preserve_terms(profile_id: str) -> frozenset[str]:
    """Derive exact preserve-as-is terms from a profile's canonical glossary.

    This deliberately understands only explicit glossary syntax. It accepts
    self-mappings (``term -> term``) and directives that start with ``keep``;
    prose, examples, aliases whose canonical output differs, and ambiguous
    mappings stay excluded.
    """
    profile = get_translation_profile(profile_id)
    if not profile:
        return frozenset()

    terms: set[str] = set()
    for raw_line in profile.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        rule = line[2:].strip()

        if "->" in rule:
            left, right = (part.strip() for part in rule.split("->", 1))
            aliases = tuple(
                term.strip() for term in re.split(r"\s*/\s*", left) if term.strip()
            )
            right_lower = right.lower()
            if right_lower.startswith("keep "):
                preserve_aliases = aliases
                if "official" in right_lower and "title" in right_lower:
                    # A standalone ordinary English word remains ambiguous even
                    # when a profile also knows a title with that spelling
                    # (for example "Again"). Require additional title shape.
                    preserve_aliases = tuple(
                        term
                        for term in aliases
                        if not re.fullmatch(r"[A-Za-z]+", term)
                    )
                    terms.update(
                        re.sub(r"\s+\([^)]*\)\s*$", "", term).strip()
                        for term in preserve_aliases
                    )
                terms.update(preserve_aliases)
                continue

            canonical = re.split(r"\s+\(", right, maxsplit=1)[0].strip()
            if canonical in aliases:
                terms.add(canonical)
            continue

        keep_match = re.match(r"^(?P<left>.+):\s*keep\b", rule, flags=re.IGNORECASE)
        if keep_match:
            terms.update(
                term.strip()
                for term in re.split(r"\s*/\s*", keep_match.group("left"))
                if term.strip()
            )

    return frozenset(term for term in terms if term)


@lru_cache(maxsize=16)
def get_translation_profile_output_terms(profile_id: str) -> frozenset[str]:
    """Return canonical outputs from the profile's explicit glossary block.

    Unlike ``get_translation_profile_preserve_terms()``, this telemetry helper
    accepts mappings whose source alias differs from the canonical output.  It
    deliberately stops at the first blank line so examples and prose cannot
    become approved output terms.
    """
    profile = get_translation_profile(profile_id)
    glossary = profile.split("\n\n", 1)[0] if profile else ""
    terms: set[str] = set()
    for raw_line in glossary.splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or "->" not in line:
            continue
        _left, right = (part.strip() for part in line[2:].split("->", 1))
        right_lower = right.lower()
        if right_lower.startswith("keep ") or any(
            marker in right_lower
            for marker in (" only when ", "; preserve ")
        ):
            continue
        canonical = re.split(r"\s+\([^)]*\)\s*$", right, maxsplit=1)[0].strip()
        if canonical:
            terms.add(canonical)
    return frozenset(terms)


@lru_cache(maxsize=16)
def get_translation_profile_mappings(profile_id: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    """Return ``(source aliases, canonical output)`` for unconditional mappings.

    Same glossary block as ``get_translation_profile_output_terms()``, but any
    rule that holds only in some contexts ("when ...") is excluded; keep
    directives are covered by ``get_translation_profile_preserve_terms()``.
    """
    profile = get_translation_profile(profile_id)
    glossary = profile.split("\n\n", 1)[0] if profile else ""
    mappings: list[tuple[tuple[str, ...], str]] = []
    for raw_line in glossary.splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or "->" not in line:
            continue
        left, right = (part.strip() for part in line[2:].split("->", 1))
        right_lower = right.lower()
        if right_lower.startswith("keep ") or any(
            marker in right_lower
            for marker in (" when ", "; preserve ")
        ):
            continue
        canonical = re.split(r"\s+\([^)]*\)\s*$", right, maxsplit=1)[0].strip()
        aliases = tuple(term.strip() for term in re.split(r"\s*/\s*", left) if term.strip())
        if canonical and aliases:
            mappings.append((aliases, canonical))
    return tuple(mappings)


def translation_profile_ids() -> frozenset[str]:
    return frozenset(_STREAMER_PROFILES)
