"""Deterministic, source-grounded escrow for narrowly evidenced unknown names.

This policy is intentionally separate from known canonical obligations.  It
does not discover names generically: only reviewed production/benchmark
surfaces and their observed grammatical contexts are eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_PLACEHOLDER_RE = re.compile(r"__LT_UNK_[1-9][0-9]*__")


@dataclass(frozen=True)
class UnknownNameEscrowEntry:
    source_name: str
    placeholder: str
    source_spans: tuple[tuple[int, int], ...]

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
            for alias in _FORBIDDEN_ALIASES.get(entry.source_name, ())
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
class _EvidenceRule:
    source_name: str
    allowed_suffixes: tuple[str, ...]
    allow_bare_boundary: bool = False


# Exact reviewed evidence only.  Adding a surface or context requires a
# production/benchmark case and matched false-positive controls.
_EVIDENCE_RULES = (
    _EvidenceRule("\uc0ac\uc625\uc324", ("\uc774\ub791",), allow_bare_boundary=True),
    _EvidenceRule("\ud478\ucf54", ("\ub3c4",)),
    _EvidenceRule("\ud478\uc21c", ("\uc774\uc5d0\uc694",)),
    _EvidenceRule("\ubaa8\ucc0c", ("\ud55c\ud14c", "\uc57c")),
)

# Exact unsupported identities observed in the reviewed benchmark/runtime
# evidence.  This is deliberately not a romanization detector or fuzzy match.
_FORBIDDEN_ALIASES = {
    "\uc0ac\uc625\uc324": ("\u5e2b\u7389",),
    "\ud478\ucf54": ("Fuko",),
    "\ud478\uc21c": ("\u666e\u9806",),
    "\ubaa8\ucc0c": ("Mochi", "\u83ab\u5947"),
}


def _is_hangul(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _rule_matches_at(source: str, start: int, rule: _EvidenceRule) -> bool:
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


def resolve_unknown_name_escrow(
    source: str,
    *,
    known_source_spans: tuple[tuple[int, int], ...] = (),
) -> UnknownNameEscrow:
    """Freeze exact reviewed unknown-name spans after known resolution."""
    matches: list[tuple[int, int, str]] = []
    for rule in _EVIDENCE_RULES:
        start = source.find(rule.source_name)
        while start >= 0:
            end = start + len(rule.source_name)
            overlaps_known = any(
                start < known_end and end > known_start
                for known_start, known_end in known_source_spans
            )
            if not overlaps_known and _rule_matches_at(source, start, rule):
                matches.append((start, end, rule.source_name))
            start = source.find(rule.source_name, start + 1)

    if not matches:
        return UnknownNameEscrow(source, source)

    by_name: dict[str, list[tuple[int, int]]] = {}
    for start, end, name in sorted(matches):
        by_name.setdefault(name, []).append((start, end))

    entries = tuple(
        UnknownNameEscrowEntry(
            source_name=name,
            placeholder=f"__LT_UNK_{index}__",
            source_spans=tuple(spans),
        )
        for index, (name, spans) in enumerate(by_name.items(), start=1)
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
