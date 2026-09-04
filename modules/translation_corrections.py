import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CORRECTIONS_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "translation_corrections.json"
)

# Scope value meaning "applies to every profile" in name_rendering_rules.
SHARED_NAME_SCOPE = "__shared__"


@dataclass(frozen=True)
class ReplacementGroup:
    source_terms: tuple[str, ...]
    replacements: tuple[tuple[str, str], ...]
    # match="all" in the JSON: every source term must be present (default: any).
    match_all: bool = False


@dataclass(frozen=True)
class NameRenderingRule:
    scope: str
    source_aliases: tuple[str, ...]
    wrong_forms: tuple[str, ...]
    canonical: str
    publication_policy: str = "repair_only"
    condition_id: str = "always"
    activation_policy: str = "exact_alias"
    repair_requires_name_context: bool = False


@dataclass(frozen=True)
class CanonicalObligation:
    """One source-proven authoritative rendering required for publication."""

    rule_id: str
    profile_id: str
    matched_alias: str
    source_spans: tuple[tuple[int, int], ...]
    canonical_target: str
    condition_id: str = "always"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "profile_id": self.profile_id,
            "matched_alias": self.matched_alias,
            "source_spans": [list(span) for span in self.source_spans],
            "canonical_target": self.canonical_target,
            "condition_id": self.condition_id,
        }


@dataclass(frozen=True)
class CanonicalObligationEvaluation:
    """Deterministic evidence for a candidate's canonical obligations."""

    passed: bool
    expected: tuple[str, ...]
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    obligations: tuple[CanonicalObligation, ...]
    target_occurrences: tuple[tuple[str, int], ...]
    rejection_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "expected": list(self.expected),
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "obligations": [obligation.as_dict() for obligation in self.obligations],
            "target_occurrences": {
                target: count for target, count in self.target_occurrences
            },
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class TranslationCorrectionTables:
    source_norm_shared: dict[str, str]
    source_norm_by_profile: dict[str, dict[str, str]]
    boundary_source_norm_shared: dict[str, str]
    boundary_source_norm_by_profile: dict[str, dict[str, str]]
    conditional_source_norm_shared: tuple[ReplacementGroup, ...]
    conditional_source_norm_by_profile: dict[str, tuple[ReplacementGroup, ...]]
    source_aware_target_replacements: tuple[ReplacementGroup, ...]
    profile_source_aware_target_replacements: dict[str, tuple[ReplacementGroup, ...]]
    korean_name_suffixes: frozenset[str]
    name_rendering_rules: tuple[NameRenderingRule, ...]


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _string_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{field_name} must map strings to strings")
    return dict(value)


def _replacement_groups(value: Any, field_name: str) -> tuple[ReplacementGroup, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")

    groups: list[ReplacementGroup] = []
    for index, raw_group in enumerate(value):
        group_name = f"{field_name}[{index}]"
        if not isinstance(raw_group, dict):
            raise ValueError(f"{group_name} must be an object")

        raw_replacements = raw_group.get("replacements")
        if not isinstance(raw_replacements, list):
            raise ValueError(f"{group_name}.replacements must be a list")

        replacements: list[tuple[str, str]] = []
        for replacement_index, raw_replacement in enumerate(raw_replacements):
            replacement_name = f"{group_name}.replacements[{replacement_index}]"
            if not isinstance(raw_replacement, dict):
                raise ValueError(f"{replacement_name} must be an object")
            wrong = raw_replacement.get("wrong")
            right = raw_replacement.get("right")
            if not isinstance(wrong, str) or not isinstance(right, str):
                raise ValueError(f"{replacement_name} must contain string wrong/right fields")
            replacements.append((wrong, right))

        match_mode = raw_group.get("match", "any")
        if match_mode not in ("any", "all"):
            raise ValueError(f"{group_name}.match must be \"any\" or \"all\"")

        groups.append(
            ReplacementGroup(
                source_terms=_string_tuple(raw_group.get("source_terms"), f"{group_name}.source_terms"),
                replacements=tuple(replacements),
                match_all=(match_mode == "all"),
            )
        )

    return tuple(groups)


def _name_rendering_rules(value: Any, field_name: str) -> tuple[NameRenderingRule, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")

    rules: list[NameRenderingRule] = []
    for index, raw_rule in enumerate(value):
        rule_name = f"{field_name}[{index}]"
        if not isinstance(raw_rule, dict):
            raise ValueError(f"{rule_name} must be an object")
        scope = raw_rule.get("scope")
        canonical = raw_rule.get("canonical")
        if not isinstance(scope, str) or not isinstance(canonical, str):
            raise ValueError(f"{rule_name} must contain string scope/canonical fields")
        publication_policy = raw_rule.get("publication_policy", "repair_only")
        if publication_policy not in ("repair_only", "required"):
            raise ValueError(
                f'{rule_name}.publication_policy must be "repair_only" or "required"'
            )
        condition_id = raw_rule.get("condition_id", "always")
        if not isinstance(condition_id, str):
            raise ValueError(f"{rule_name}.condition_id must be a string")
        activation_policy = raw_rule.get("activation_policy", "exact_alias")
        if activation_policy not in ("exact_alias", "name_context_required"):
            raise ValueError(
                f'{rule_name}.activation_policy must be "exact_alias" or '
                '"name_context_required"'
            )
        repair_requires_name_context = raw_rule.get(
            "repair_requires_name_context", False
        )
        if not isinstance(repair_requires_name_context, bool):
            raise ValueError(
                f"{rule_name}.repair_requires_name_context must be a boolean"
            )

        rules.append(
            NameRenderingRule(
                scope=scope,
                source_aliases=_string_tuple(raw_rule.get("source_aliases"), f"{rule_name}.source_aliases"),
                wrong_forms=_string_tuple(raw_rule.get("wrong_forms"), f"{rule_name}.wrong_forms"),
                canonical=canonical,
                publication_policy=publication_policy,
                condition_id=condition_id,
                activation_policy=activation_policy,
                repair_requires_name_context=repair_requires_name_context,
            )
        )

    return tuple(rules)


def _is_hangul_syllable(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _is_name_suffix_boundary(char: str) -> bool:
    return char.isspace() or not char.isalnum()


def _source_alias_matches_at(
    source: str,
    alias: str,
    start: int,
    korean_name_suffixes: frozenset[str],
) -> bool:
    """Mirror the live name-rendering source-boundary contract."""
    if start > 0 and _is_hangul_syllable(source[start - 1]):
        return False

    end = start + len(alias)
    if end >= len(source):
        return True
    if not _is_hangul_syllable(source[end]):
        return True

    suffix_end = end
    while suffix_end < len(source) and _is_hangul_syllable(source[suffix_end]):
        suffix_end += 1
    suffix = source[end:suffix_end]
    if suffix not in korean_name_suffixes:
        return False
    return suffix_end >= len(source) or _is_name_suffix_boundary(source[suffix_end])


_NAME_CONTEXT_HONORIFICS = ("언니", "씨", "님")
_NAME_CONTEXT_ATTACHED_SUFFIXES = ("이가", "이는", "아", "이")


def _source_alias_has_name_context(
    source: str,
    alias: str,
    start: int,
    korean_name_suffixes: frozenset[str],
) -> bool:
    """Recognize a narrow deterministic Korean name-use context.

    Honorifics may be separated by whitespace and may themselves take a Korean
    particle (for example ``언니보다``).  Short vocative/subject suffixes must
    be attached to the alias and end at a non-Hangul boundary.
    """
    if start > 0 and _is_hangul_syllable(source[start - 1]):
        return False

    end = start + len(alias)
    context_start = end
    while context_start < len(source) and source[context_start].isspace():
        context_start += 1

    for honorific in _NAME_CONTEXT_HONORIFICS:
        if source.startswith(honorific, context_start):
            honorific_end = context_start + len(honorific)
            if honorific_end >= len(source) or not _is_hangul_syllable(
                source[honorific_end]
            ):
                return True
            particle_end = honorific_end
            while particle_end < len(source) and _is_hangul_syllable(
                source[particle_end]
            ):
                particle_end += 1
            particle = source[honorific_end:particle_end]
            if particle in korean_name_suffixes and (
                particle_end >= len(source)
                or _is_name_suffix_boundary(source[particle_end])
            ):
                return True

    if context_start != end:
        return False
    for suffix in _NAME_CONTEXT_ATTACHED_SUFFIXES:
        if not source.startswith(suffix, end):
            continue
        suffix_end = end + len(suffix)
        if suffix_end >= len(source) or not _is_hangul_syllable(source[suffix_end]):
            return True
    return False


def source_alias_matches(
    source: str,
    alias: str,
    *,
    activation_policy: str = "exact_alias",
    korean_name_suffixes: frozenset[str],
) -> bool:
    """Apply one name rule's boundary and collision policy consistently."""
    start = source.find(alias)
    while start >= 0:
        if activation_policy == "name_context_required":
            matched = _source_alias_has_name_context(
                source, alias, start, korean_name_suffixes
            )
        else:
            matched = _source_alias_matches_at(
                source, alias, start, korean_name_suffixes
            )
        if matched:
            return True
        start = source.find(alias, start + 1)
    return False


def resolve_canonical_obligations(
    source_text: str,
    *,
    profile_id: str,
    profile_applied: bool,
    rules: tuple[NameRenderingRule, ...],
    korean_name_suffixes: frozenset[str],
) -> tuple[CanonicalObligation, ...]:
    """Resolve v1 hard obligations from one normalized production source.

    V1 is deliberately narrow: exact active-profile rules, explicit opt-in,
    ``condition_id=always``, and exactly one non-overlapping source occurrence.
    """
    if not source_text or not profile_applied or not profile_id:
        return ()

    obligations: list[CanonicalObligation] = []
    for rule in rules:
        if (
            rule.scope != profile_id
            or rule.publication_policy != "required"
            or rule.condition_id != "always"
        ):
            continue

        matches_by_start: dict[int, tuple[int, str]] = {}
        for alias in sorted(set(rule.source_aliases), key=len, reverse=True):
            if not alias:
                continue
            start = source_text.find(alias)
            while start >= 0:
                if rule.activation_policy == "name_context_required":
                    source_match = _source_alias_has_name_context(
                        source_text, alias, start, korean_name_suffixes
                    )
                else:
                    source_match = _source_alias_matches_at(
                        source_text, alias, start, korean_name_suffixes
                    )
                if source_match:
                    end = start + len(alias)
                    current = matches_by_start.get(start)
                    if current is None or end > current[0]:
                        matches_by_start[start] = (end, alias)
                start = source_text.find(alias, start + 1)

        matches = sorted(
            (start, end, alias)
            for start, (end, alias) in matches_by_start.items()
        )
        non_overlapping: list[tuple[int, int, str]] = []
        for match in matches:
            if non_overlapping and match[0] < non_overlapping[-1][1]:
                continue
            non_overlapping.append(match)
        if len(non_overlapping) != 1:
            continue

        start, end, alias = non_overlapping[0]
        obligations.append(
            CanonicalObligation(
                rule_id=f"name:{rule.scope}:{rule.canonical}",
                profile_id=profile_id,
                matched_alias=alias,
                source_spans=((start, end),),
                canonical_target=rule.canonical,
                condition_id=rule.condition_id,
            )
        )
    return tuple(obligations)


def evaluate_canonical_obligations(
    target_text: str | None,
    obligations: tuple[CanonicalObligation, ...],
) -> CanonicalObligationEvaluation:
    """Check presence only; never insert or repair a missing canonical target."""
    target = target_text or ""
    expected = tuple(dict.fromkeys(item.canonical_target for item in obligations))

    def exact_occurrence_count(term: str) -> int:
        count = 0
        start = target.find(term)
        contains_latin = any(char.isascii() and char.isalpha() for char in term)
        contains_hangul = any(_is_hangul_syllable(char) for char in term)
        while start >= 0:
            end = start + len(term)
            left = target[start - 1] if start > 0 else ""
            right = target[end] if end < len(target) else ""
            bounded = True
            if contains_latin:
                bounded = not (
                    left.isascii() and left.isalpha()
                    or right.isascii() and right.isalpha()
                )
            elif contains_hangul:
                # A following Korean particle is valid (e.g. 모카가), while a
                # preceding Hangul syllable proves this is embedded in another
                # lexical item rather than the authoritative name span.
                bounded = not _is_hangul_syllable(left)
            if bounded:
                count += 1
            start = target.find(term, start + 1)
        return count

    occurrences = tuple((term, exact_occurrence_count(term)) for term in expected)
    satisfied = tuple(term for term, count in occurrences if count > 0)
    missing = tuple(term for term, count in occurrences if count <= 0)
    return CanonicalObligationEvaluation(
        passed=not missing,
        expected=expected,
        satisfied=satisfied,
        missing=missing,
        obligations=obligations,
        target_occurrences=occurrences,
        rejection_reason="" if not missing else "canonical_obligation_missing",
    )


def load_translation_corrections(
    path: Path = _CORRECTIONS_DATA_PATH,
) -> TranslationCorrectionTables:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("translation corrections data must be a JSON object")

    raw_source_norm = data.get("source_norm")
    if not isinstance(raw_source_norm, dict):
        raise ValueError("source_norm must be an object")

    raw_profiles = raw_source_norm.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError("source_norm.profiles must be an object")

    raw_boundary_source_norm = data.get("boundary_source_norm")
    if not isinstance(raw_boundary_source_norm, dict):
        raise ValueError("boundary_source_norm must be an object")

    raw_boundary_profiles = raw_boundary_source_norm.get("profiles")
    if not isinstance(raw_boundary_profiles, dict):
        raise ValueError("boundary_source_norm.profiles must be an object")

    raw_conditional_source_norm = data.get("conditional_source_norm")
    if not isinstance(raw_conditional_source_norm, dict):
        raise ValueError("conditional_source_norm must be an object")

    raw_conditional_profiles = raw_conditional_source_norm.get("profiles")
    if not isinstance(raw_conditional_profiles, dict):
        raise ValueError("conditional_source_norm.profiles must be an object")

    profile_source_aware = data.get("profile_source_aware_target_replacements")
    if not isinstance(profile_source_aware, dict):
        raise ValueError("profile_source_aware_target_replacements must be an object")

    return TranslationCorrectionTables(
        source_norm_shared=_string_map(raw_source_norm.get("shared"), "source_norm.shared"),
        source_norm_by_profile={
            profile: _string_map(values, f"source_norm.profiles.{profile}")
            for profile, values in raw_profiles.items()
            if isinstance(profile, str)
        },
        boundary_source_norm_shared=_string_map(
            raw_boundary_source_norm.get("shared"),
            "boundary_source_norm.shared",
        ),
        boundary_source_norm_by_profile={
            profile: _string_map(
                values,
                f"boundary_source_norm.profiles.{profile}",
            )
            for profile, values in raw_boundary_profiles.items()
            if isinstance(profile, str)
        },
        conditional_source_norm_shared=_replacement_groups(
            raw_conditional_source_norm.get("shared"),
            "conditional_source_norm.shared",
        ),
        conditional_source_norm_by_profile={
            profile: _replacement_groups(
                groups,
                f"conditional_source_norm.profiles.{profile}",
            )
            for profile, groups in raw_conditional_profiles.items()
            if isinstance(profile, str)
        },
        source_aware_target_replacements=_replacement_groups(
            data.get("source_aware_target_replacements"),
            "source_aware_target_replacements",
        ),
        profile_source_aware_target_replacements={
            profile: _replacement_groups(groups, f"profile_source_aware_target_replacements.{profile}")
            for profile, groups in profile_source_aware.items()
            if isinstance(profile, str)
        },
        korean_name_suffixes=frozenset(
            _string_tuple(data.get("korean_name_suffixes"), "korean_name_suffixes")
        ),
        name_rendering_rules=_name_rendering_rules(
            data.get("name_rendering_rules"),
            "name_rendering_rules",
        ),
    )
