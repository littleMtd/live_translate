import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CORRECTIONS_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "translation_corrections.json"
)


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


@dataclass(frozen=True)
class TranslationCorrectionTables:
    source_norm_shared: dict[str, str]
    source_norm_by_profile: dict[str, dict[str, str]]
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

        rules.append(
            NameRenderingRule(
                scope=scope,
                source_aliases=_string_tuple(raw_rule.get("source_aliases"), f"{rule_name}.source_aliases"),
                wrong_forms=_string_tuple(raw_rule.get("wrong_forms"), f"{rule_name}.wrong_forms"),
                canonical=canonical,
            )
        )

    return tuple(rules)


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
