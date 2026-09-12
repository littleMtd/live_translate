"""Deterministic, source-grounded escrow for narrowly evidenced unknown names.

This policy is intentionally separate from known canonical obligations.  It
does not discover names generically: only reviewed production/benchmark
surfaces and their observed grammatical contexts are eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


_PLACEHOLDER_RE = re.compile(r"__LT_UNK_[1-9][0-9]*__")
_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "unknown_name_escrow.json"
_HANGUL_NAME_RE = re.compile(r"^[가-힣]+$")


@dataclass(frozen=True)
class UnknownNameEscrowEntry:
    source_name: str
    placeholder: str
    source_spans: tuple[tuple[int, int], ...]
    forbidden_aliases: tuple[str, ...] = ()

    @property
    def expected_count(self) -> int:
        return len(self.source_spans)


@dataclass(frozen=True)
class UnknownNameEscrowEvaluation:
    passed: bool
    reason: str
    expected: tuple[str, ...]
    missing: tuple[str, ...]
    duplicated: tuple[str, ...]
    mutated_placeholder: bool = False
    invented_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnknownNameEscrow:
    original_source: str
    provider_source: str
    entries: tuple[UnknownNameEscrowEntry, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.entries)

    @property
    def approved_hangul_terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(entry.source_name for entry in self.entries))

    def evaluate_provider_candidate(
        self, candidate: str | None
    ) -> UnknownNameEscrowEvaluation:
        value = candidate or ""
        if not self.entries:
            return UnknownNameEscrowEvaluation(True, "", (), (), ())

        expected = tuple(entry.placeholder for entry in self.entries)
        missing: list[str] = []
        duplicated: list[str] = []
        for entry in self.entries:
            count = value.count(entry.placeholder)
            if count < entry.expected_count:
                missing.append(entry.placeholder)
            elif count > entry.expected_count:
                duplicated.append(entry.placeholder)

        placeholder_residue = value
        for placeholder in expected:
            placeholder_residue = placeholder_residue.replace(placeholder, "")
        observed = set(_PLACEHOLDER_RE.findall(placeholder_residue))
        mutated = bool(observed or "__LT_" in placeholder_residue)
        invented_aliases = tuple(
            alias
            for entry in self.entries
            for alias in entry.forbidden_aliases
            if alias.casefold() in value.casefold()
        )
        passed = not missing and not duplicated and not mutated and not invented_aliases
        return UnknownNameEscrowEvaluation(
            passed=passed,
            reason="" if passed else "unknown_name_placeholder_invalid",
            expected=expected,
            missing=tuple(missing),
            duplicated=tuple(duplicated),
            mutated_placeholder=mutated,
            invented_aliases=invented_aliases,
        )

    def restore_provider_candidate(self, candidate: str) -> str:
        restored = candidate
        for entry in self.entries:
            restored = restored.replace(entry.placeholder, entry.source_name)
        return restored

    def evaluate_final(self, target: str | None) -> UnknownNameEscrowEvaluation:
        value = target or ""
        if not self.entries:
            return UnknownNameEscrowEvaluation(True, "", (), (), ())

        expected = tuple(entry.source_name for entry in self.entries)
        missing: list[str] = []
        duplicated: list[str] = []
        for entry in self.entries:
            count = value.count(entry.source_name)
            if count < entry.expected_count:
                missing.append(entry.source_name)
            elif count > entry.expected_count:
                duplicated.append(entry.source_name)
        mutated = bool(_PLACEHOLDER_RE.search(value) or "__LT_" in value)
        passed = not missing and not duplicated and not mutated
        return UnknownNameEscrowEvaluation(
            passed=passed,
            reason="" if passed else "unknown_name_final_invariant_failed",
            expected=expected,
            missing=tuple(missing),
            duplicated=tuple(duplicated),
            mutated_placeholder=mutated,
        )


@dataclass(frozen=True)
class ExactNameEvidenceRule:
    source_name: str
    allowed_suffixes: tuple[str, ...]
    allow_bare_boundary: bool = False
    forbidden_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExplicitIdentityContextRule:
    rule_id: str
    prefixes: tuple[str, ...]
    suffixes: tuple[str, ...]
    min_syllables: int
    max_syllables: int
    excluded_candidates: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SourceGroundedReferentContextRule:
    rule_id: str
    suffixes: tuple[str, ...]
    min_syllables: int
    max_syllables: int
    excluded_candidates: frozenset[str] = frozenset()


@dataclass(frozen=True)
class UnknownNamePolicy:
    schema_version: int
    exact_rules: tuple[ExactNameEvidenceRule, ...]
    identity_contexts: tuple[ExplicitIdentityContextRule, ...]
    referent_contexts: tuple[SourceGroundedReferentContextRule, ...] = ()


def _strings(value: Any, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    result = tuple(value)
    if nonempty and (not result or any(not item.strip() for item in result)):
        raise ValueError(f"{field} must contain non-empty strings")
    if any("__LT_" in item for item in result):
        raise ValueError(f"{field} must not contain placeholder syntax")
    return result


def load_unknown_name_policy(path: Path = _DATA_PATH) -> UnknownNamePolicy:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
        raise ValueError("unknown-name escrow requires schema_version 1 or 2")
    raw_exact = data.get("reviewed_exact_names")
    raw_contexts = data.get("explicit_identity_contexts")
    if not isinstance(raw_exact, list) or not isinstance(raw_contexts, list):
        raise ValueError("unknown-name escrow rules must be lists")

    exact_rules: list[ExactNameEvidenceRule] = []
    seen_names: set[str] = set()
    for index, row in enumerate(raw_exact):
        field = f"reviewed_exact_names[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{field} must be an object")
        name = row.get("source_name")
        if not isinstance(name, str) or not _HANGUL_NAME_RE.fullmatch(name):
            raise ValueError(f"{field}.source_name must contain only Hangul syllables")
        if name in seen_names:
            raise ValueError(f"duplicate reviewed unknown name: {name}")
        seen_names.add(name)
        allow_bare = row.get("allow_bare_boundary", False)
        if not isinstance(allow_bare, bool):
            raise ValueError(f"{field}.allow_bare_boundary must be a boolean")
        suffixes = _strings(row.get("allowed_suffixes", []), f"{field}.allowed_suffixes")
        if not allow_bare and not suffixes:
            raise ValueError(f"{field} requires a suffix or bare-boundary activation")
        exact_rules.append(ExactNameEvidenceRule(
            name,
            suffixes,
            allow_bare,
            _strings(row.get("forbidden_aliases", []), f"{field}.forbidden_aliases"),
        ))

    context_rules: list[ExplicitIdentityContextRule] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(raw_contexts):
        field = f"explicit_identity_contexts[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{field} must be an object")
        rule_id = row.get("rule_id")
        minimum = row.get("min_syllables")
        maximum = row.get("max_syllables")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_ids:
            raise ValueError(f"{field}.rule_id must be unique and non-empty")
        if not isinstance(minimum, int) or not isinstance(maximum, int) or not (1 <= minimum <= maximum <= 12):
            raise ValueError(f"{field} has invalid syllable bounds")
        seen_ids.add(rule_id)
        excluded = _strings(row.get("excluded_candidates", []), f"{field}.excluded_candidates")
        if any(not _HANGUL_NAME_RE.fullmatch(value) for value in excluded):
            raise ValueError(f"{field}.excluded_candidates must contain only Hangul syllables")
        context_rules.append(ExplicitIdentityContextRule(
            rule_id,
            _strings(row.get("prefixes"), f"{field}.prefixes", nonempty=True),
            _strings(row.get("suffixes"), f"{field}.suffixes", nonempty=True),
            minimum,
            maximum,
            frozenset(excluded),
        ))
    referent_rules: list[SourceGroundedReferentContextRule] = []
    raw_referent_contexts = data.get("source_grounded_referent_contexts", [])
    if not isinstance(raw_referent_contexts, list):
        raise ValueError("source-grounded referent contexts must be a list")
    for index, row in enumerate(raw_referent_contexts):
        field = f"source_grounded_referent_contexts[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{field} must be an object")
        rule_id = row.get("rule_id")
        minimum = row.get("min_syllables")
        maximum = row.get("max_syllables")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_ids:
            raise ValueError(f"{field}.rule_id must be unique and non-empty")
        if not isinstance(minimum, int) or not isinstance(maximum, int) or not (
            2 <= minimum <= maximum <= 12
        ):
            raise ValueError(f"{field} has invalid syllable bounds")
        seen_ids.add(rule_id)
        excluded = _strings(
            row.get("excluded_candidates", []), f"{field}.excluded_candidates"
        )
        if any(not _HANGUL_NAME_RE.fullmatch(value) for value in excluded):
            raise ValueError(
                f"{field}.excluded_candidates must contain only Hangul syllables"
            )
        referent_rules.append(SourceGroundedReferentContextRule(
            rule_id=rule_id,
            suffixes=_strings(
                row.get("suffixes"), f"{field}.suffixes", nonempty=True
            ),
            min_syllables=minimum,
            max_syllables=maximum,
            excluded_candidates=frozenset(excluded),
        ))
    return UnknownNamePolicy(
        int(data["schema_version"]),
        tuple(exact_rules),
        tuple(context_rules),
        tuple(referent_rules),
    )


UNKNOWN_NAME_POLICY = load_unknown_name_policy()


def _is_hangul(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _rule_matches_at(source: str, start: int, rule: ExactNameEvidenceRule) -> bool:
    end = start + len(rule.source_name)
    if start > 0 and _is_hangul(source[start - 1]):
        return False
    if rule.allow_bare_boundary and (end >= len(source) or not _is_hangul(source[end])):
        return True
    for suffix in rule.allowed_suffixes:
        suffix_end = end + len(suffix)
        if source.startswith(suffix, end) and (
            suffix_end >= len(source) or not _is_hangul(source[suffix_end])
        ):
            return True
    return False


def _identity_context_matches(
    source: str,
    rule: ExplicitIdentityContextRule,
) -> tuple[tuple[int, int, str], ...]:
    suffix_pattern = "|".join(
        re.escape(value) for value in sorted(rule.suffixes, key=len, reverse=True)
    )
    matches: list[tuple[int, int, str]] = []
    for prefix in rule.prefixes:
        pattern = re.compile(
            rf"(?<![가-힣]){re.escape(prefix)}\s+"
            rf"(?P<name>[가-힣]{{{rule.min_syllables},{rule.max_syllables}}}?)"
            rf"(?:{suffix_pattern})(?![가-힣])"
        )
        for match in pattern.finditer(source):
            name = match.group("name")
            if name not in rule.excluded_candidates:
                matches.append((match.start("name"), match.end("name"), name))
    return tuple(matches)


def _referent_context_matches(
    source: str,
    rule: SourceGroundedReferentContextRule,
) -> tuple[tuple[int, int, str], ...]:
    suffix_pattern = "|".join(
        re.escape(value) for value in sorted(rule.suffixes, key=len, reverse=True)
    )
    continuation_pattern = "|".join(
        re.escape(value)
        for value in (
            "이라고", "라며", "인데", "였다", "가", "는",
            "를", "도", "와", "과", "의", "로",
        )
    )
    pattern = re.compile(
        rf"(?<![가-힣])"
        rf"(?P<name>[가-힣]{{{rule.min_syllables},{rule.max_syllables}}}?)"
        rf"(?:{suffix_pattern})"
        rf"(?=$|[^가-힣]|(?:{continuation_pattern})(?=$|[^가-힣]))"
    )
    return tuple(
        (match.start("name"), match.end("name"), match.group("name"))
        for match in pattern.finditer(source)
        if match.group("name") not in rule.excluded_candidates
    )


def resolve_unknown_name_escrow(
    source: str,
    *,
    known_source_spans: tuple[tuple[int, int], ...] = (),
    policy: UnknownNamePolicy = UNKNOWN_NAME_POLICY,
) -> UnknownNameEscrow:
    """Freeze exact reviewed unknown-name spans after known resolution."""
    matches: list[tuple[int, int, str, tuple[str, ...]]] = []
    for rule in policy.exact_rules:
        start = source.find(rule.source_name)
        while start >= 0:
            end = start + len(rule.source_name)
            overlaps_known = any(
                start < known_end and end > known_start
                for known_start, known_end in known_source_spans
            )
            if not overlaps_known and _rule_matches_at(source, start, rule):
                matches.append((start, end, rule.source_name, rule.forbidden_aliases))
            start = source.find(rule.source_name, start + 1)

    for rule in policy.identity_contexts:
        for start, end, name in _identity_context_matches(source, rule):
            overlaps_known = any(
                start < known_end and end > known_start
                for known_start, known_end in known_source_spans
            )
            if not overlaps_known:
                matches.append((start, end, name, ()))

    for rule in policy.referent_contexts:
        for start, end, name in _referent_context_matches(source, rule):
            overlaps_known = any(
                start < known_end and end > known_start
                for known_start, known_end in known_source_spans
            )
            if not overlaps_known:
                matches.append((start, end, name, ()))

    if not matches:
        return UnknownNameEscrow(source, source)

    merged_matches: dict[tuple[int, int, str], tuple[str, ...]] = {}
    for start, end, name, forbidden_aliases in matches:
        key = (start, end, name)
        merged_matches[key] = tuple(dict.fromkeys(
            (*merged_matches.get(key, ()), *forbidden_aliases)
        ))
    matches = [(*key, aliases) for key, aliases in merged_matches.items()]
    safe_matches = [
        match
        for match in matches
        if not any(
            match[0] < other[1]
            and match[1] > other[0]
            and match[2] != other[2]
            for other in matches
        )
    ]
    by_name: dict[str, tuple[list[tuple[int, int]], tuple[str, ...]]] = {}
    for start, end, name, forbidden_aliases in sorted(safe_matches):
        spans, aliases = by_name.setdefault(name, ([], forbidden_aliases))
        spans.append((start, end))
        if not aliases and forbidden_aliases:
            by_name[name] = (spans, forbidden_aliases)

    entries = tuple(
        UnknownNameEscrowEntry(
            source_name=name,
            placeholder=f"__LT_UNK_{index}__",
            source_spans=tuple(spans),
            forbidden_aliases=forbidden_aliases,
        )
        for index, (name, (spans, forbidden_aliases)) in enumerate(
            by_name.items(), start=1
        )
    )
    replacements = [
        (start, end, entry.placeholder)
        for entry in entries
        for start, end in entry.source_spans
    ]
    provider_source = source
    for start, end, placeholder in sorted(replacements, reverse=True):
        provider_source = provider_source[:start] + placeholder + provider_source[end:]
    return UnknownNameEscrow(source, provider_source, entries)
