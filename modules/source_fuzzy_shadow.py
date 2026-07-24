"""Conservative, profile-scoped source fuzzy matching for record-only telemetry."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from modules.streamer_profiles import canonical_profile_id, profile_stt_terms
from modules.translation_corrections import SHARED_NAME_SCOPE, load_translation_corrections


_HANGUL_TOKEN_RE = re.compile(r"[가-힣]{2,12}")
_HANGUL_TERM_RE = re.compile(r"[가-힣]{2,12}")
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MAX_NORMALIZED_DISTANCE = 0.20
_MAX_ABSOLUTE_DISTANCE = 1
_MIN_RUNNER_UP_MARGIN = 0.10
_VOCATIVE_SUFFIXES = ("아", "야")
_FAN_TERMS_PATH = Path(__file__).resolve().parents[1] / "data" / "fan_terms.json"
_CORRECTION_TABLES = load_translation_corrections()


def _enabled() -> bool:
    return (
        os.getenv("LIVE_TRANSLATE_SOURCE_FUZZY_SHADOW", "1").strip().lower()
        not in _FALSE_VALUES
    )


def _jamo(text: str) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFD", unicodedata.normalize("NFC", text)))


def _edit_distance(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for row, left_item in enumerate(left, 1):
        current = [row]
        for column, right_item in enumerate(right, 1):
            substitution = previous[column - 1] + (left_item != right_item)
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    substitution,
                )
            )
        previous = current
    return previous[-1]


def _distance(left: str, right: str) -> tuple[int, float]:
    left_jamo = _jamo(left)
    right_jamo = _jamo(right)
    absolute = _edit_distance(left_jamo, right_jamo)
    return absolute, absolute / max(len(left_jamo), len(right_jamo), 1)


@lru_cache(maxsize=None)
def _fan_term_entries(profile_id: str) -> tuple[dict[str, object], ...]:
    try:
        data = json.loads(_FAN_TERMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    entries = data.get("fan_terms") if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return ()
    return tuple(
        entry
        for entry in entries
        if isinstance(entry, dict)
        and canonical_profile_id(str(entry.get("profile_id") or "")) == profile_id
    )


@lru_cache(maxsize=None)
def _reviewed_profile_canonicals(profile_id: str) -> tuple[str, ...]:
    """Return reviewed canonicals that also belong to this profile's glossary."""
    glossary = {
        unicodedata.normalize("NFC", term.strip())
        for term in profile_stt_terms(profile_id)
        if term.strip()
    }
    reviewed = {
        unicodedata.normalize("NFC", term.strip())
        for term in _CORRECTION_TABLES.source_norm_by_profile.get(profile_id, {}).values()
        if term.strip()
    }
    reviewed.update(
        unicodedata.normalize("NFC", str(entry.get("term") or "").strip())
        for entry in _fan_term_entries(profile_id)
    )
    return tuple(sorted(glossary.intersection(reviewed)))


@lru_cache(maxsize=None)
def _all_reviewed_profile_forms(profile_id: str) -> frozenset[str]:
    """Return every reviewed source form so aliases never become fuzzy misses."""
    forms = {
        unicodedata.normalize("NFC", term.strip())
        for term in profile_stt_terms(profile_id)
        if term.strip()
    }
    forms.update(
        unicodedata.normalize("NFC", term.strip())
        for term in _CORRECTION_TABLES.source_norm_by_profile.get(profile_id, {})
        if term.strip()
    )
    forms.update(
        unicodedata.normalize("NFC", alias.strip())
        for rule in _CORRECTION_TABLES.name_rendering_rules
        if rule.scope in {profile_id, SHARED_NAME_SCOPE}
        for alias in rule.source_aliases
        if alias.strip()
    )
    for entry in _fan_term_entries(profile_id):
        term = unicodedata.normalize(
            "NFC", str(entry.get("term") or "").strip()
        )
        if term:
            forms.add(term)
        aliases = entry.get("aliases")
        if isinstance(aliases, list):
            forms.update(
                unicodedata.normalize("NFC", str(alias).strip())
                for alias in aliases
                if str(alias).strip()
            )
    return frozenset(term for term in forms if _HANGUL_TERM_RE.fullmatch(term))


def _profile_terms(profile_id: str, terms: Iterable[str] | None) -> tuple[str, ...]:
    # Explicit terms are a test/offline injection point and are assumed to have
    # already passed the same canonical review. Production uses the reviewed
    # intersection, never every glossary alias.
    raw_terms = (
        _reviewed_profile_canonicals(profile_id)
        if terms is None
        else tuple(terms)
    )
    normalized = (
        unicodedata.normalize("NFC", str(term).strip())
        for term in raw_terms
    )
    return tuple(
        dict.fromkeys(
            term
            for term in normalized
            if _HANGUL_TERM_RE.fullmatch(term)
        )
    )


def build_source_fuzzy_shadow(
    text: str,
    *,
    profile_id: str,
    use_profile: bool,
    terms: Iterable[str] | None = None,
    enabled: bool | None = None,
) -> dict[str, object]:
    """Return a counterfactual proposal without changing ``text``.

    Only exact-length Hangul tokens and reviewed canonical terms from the
    active profile glossary are compared. A proposal requires one jamo edit at
    most, normalized distance <= 0.20, and a clear runner-up margin. Ambiguous
    rows remain observable but never contribute to ``proposed_text``.
    """
    source = unicodedata.normalize("NFC", str(text or ""))
    active = _enabled() if enabled is None else bool(enabled)
    canonical_profile = canonical_profile_id(profile_id) if use_profile else ""
    glossary_terms = (
        _profile_terms(canonical_profile, terms)
        if active and use_profile and canonical_profile
        else ()
    )
    known_profile_terms = (
        _all_reviewed_profile_forms(canonical_profile)
        if terms is None and canonical_profile
        else frozenset(glossary_terms)
    )
    candidates: list[dict[str, object]] = []
    replacements: list[tuple[int, int, str]] = []

    for match in _HANGUL_TOKEN_RE.finditer(source):
        observed = match.group(0)
        # Already-listed aliases are not fuzzy misses. Existing reviewed
        # deterministic normalization owns them, and comparing one listed
        # alias against another can invent a bogus canonical direction.
        if observed in known_profile_terms:
            continue
        ranked: list[tuple[float, int, str]] = []
        for canonical in glossary_terms:
            if canonical == observed or len(canonical) != len(observed):
                continue
            absolute, normalized = _distance(observed, canonical)
            # "해둥아" is a valid vocative form of "해둥이", not an STT miss.
            # Do not reinterpret a final-syllable vocative as a name variant.
            if (
                observed.endswith(_VOCATIVE_SUFFIXES)
                and observed[:-1] == canonical[:-1]
            ):
                continue
            if (
                absolute <= _MAX_ABSOLUTE_DISTANCE
                and normalized <= _MAX_NORMALIZED_DISTANCE
            ):
                ranked.append((normalized, absolute, canonical))
        if not ranked:
            continue
        ranked.sort(key=lambda row: (row[0], row[1], row[2]))
        best_normalized, best_absolute, best_term = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        margin = (
            runner_up[0] - best_normalized
            if runner_up is not None
            else 1.0
        )
        unique = runner_up is None or margin >= _MIN_RUNNER_UP_MARGIN
        candidates.append(
            {
                "observed": observed,
                "start": match.start(),
                "end": match.end(),
                "decision": "unique_match" if unique else "ambiguous",
                "canonical": best_term if unique else "",
                "best_candidate": best_term,
                "absolute_distance": best_absolute,
                "normalized_distance": round(best_normalized, 4),
                "runner_up": runner_up[2] if runner_up is not None else "",
                "runner_up_normalized_distance": (
                    round(runner_up[0], 4) if runner_up is not None else None
                ),
                "runner_up_margin": round(margin, 4),
            }
        )
        if unique:
            replacements.append((match.start(), match.end(), best_term))

    proposed = source
    for start, end, canonical in reversed(replacements):
        proposed = proposed[:start] + canonical + proposed[end:]

    if not active:
        reason = "disabled"
    elif not use_profile:
        reason = "profile_disabled"
    elif not canonical_profile:
        reason = "no_active_profile"
    elif not glossary_terms:
        reason = "no_profile_hangul_terms"
    elif candidates:
        reason = "candidates_observed"
    else:
        reason = "no_candidate"

    return {
        "schema": 1,
        "mode": "record_only",
        "enabled": active,
        "applied": False,
        "profile_id": canonical_profile,
        "eligible": bool(active and use_profile and canonical_profile and glossary_terms),
        "reason": reason,
        "profile_term_count": len(glossary_terms),
        "candidate_count": len(candidates),
        "unique_match_count": sum(
            candidate["decision"] == "unique_match"
            for candidate in candidates
        ),
        "ambiguous_count": sum(
            candidate["decision"] == "ambiguous"
            for candidate in candidates
        ),
        "would_change": proposed != source,
        "proposed_text": proposed if candidates else "",
        "candidates": candidates,
    }


def safe_source_fuzzy_shadow(
    text: str,
    *,
    profile_id: str,
    use_profile: bool,
) -> dict[str, object]:
    """Fail closed so record-only diagnostics can never block translation."""
    try:
        return build_source_fuzzy_shadow(
            text,
            profile_id=profile_id,
            use_profile=use_profile,
        )
    except Exception as exc:
        return {
            "schema": 1,
            "mode": "record_only",
            "enabled": False,
            "applied": False,
            "profile_id": "",
            "eligible": False,
            "reason": "diagnostic_error",
            "error_type": type(exc).__name__,
            "profile_term_count": 0,
            "candidate_count": 0,
            "unique_match_count": 0,
            "ambiguous_count": 0,
            "would_change": False,
            "proposed_text": "",
            "candidates": [],
        }
