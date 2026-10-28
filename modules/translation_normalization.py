"""Translation text rules moved out of translator.py: source normalization before matching,
source-aware target corrections, name rendering, canonical-name obligations and the
per-request correction log. Behaviour is unchanged; translator.py re-exports these names."""
import re
import threading
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from opencc import OpenCC
from zhconv import convert as convert_chinese
from config import cfg
from modules.activity_context import effective_profile_id
from modules.profile_context import effective_profile_applied
from modules.translation_prompts import get_translation_profile_preserve_terms
from modules.streamer_profiles import common_stt_terms
from modules.request_protection import (
    ProtectedSourceSpan,
    RequestProtection,
    resolve_request_protection,
)
from modules.entity_registry import ENTITY_REGISTRY, EntityActivation
from modules.translation_corrections import (
    CanonicalObligation,
    NameRenderingRule as _NameRenderingRule,
    SHARED_NAME_SCOPE as _SHARED_NAME_SCOPE,
    load_translation_corrections,
    source_alias_matches,
    source_alias_matches_at,
)

_DEPENDENCY_MARKER_BOUNDARY_RE = re.compile(r"^[\s\.,!?~…。？！,，、:;；]|$")
_COMMON_PRESERVED_ACRONYMS = frozenset(
    term
    for term in common_stt_terms()
    if re.fullmatch(r"[A-Z][A-Z0-9&:+./-]{1,15}", term)
)
_CORRECTION_TABLES = load_translation_corrections()
_SOURCE_NORM_SHARED = _CORRECTION_TABLES.source_norm_shared
_SOURCE_NORM_BY_PROFILE = _CORRECTION_TABLES.source_norm_by_profile
_BOUNDARY_SOURCE_NORM_SHARED = _CORRECTION_TABLES.boundary_source_norm_shared
_BOUNDARY_SOURCE_NORM_BY_PROFILE = _CORRECTION_TABLES.boundary_source_norm_by_profile
_TARGET_SCRIPT_NORMALIZATION = tuple(
    _CORRECTION_TABLES.target_script_normalization.items()
)
_CONDITIONAL_SOURCE_NORM_SHARED = tuple(
    (group.source_terms, group.replacements, group.match_all)
    for group in _CORRECTION_TABLES.conditional_source_norm_shared
)
_CONDITIONAL_SOURCE_NORM_BY_PROFILE = {
    profile: tuple(
        (group.source_terms, group.replacements, group.match_all) for group in groups
    )
    for profile, groups in _CORRECTION_TABLES.conditional_source_norm_by_profile.items()
}
_SOURCE_AWARE_TARGET_REPLACEMENTS = tuple(
    (group.source_terms, group.replacements, group.match_all)
    for group in _CORRECTION_TABLES.source_aware_target_replacements
)
_PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS = {
    profile: tuple(
        (group.source_terms, group.replacements, group.match_all) for group in groups
    )
    for profile, groups in _CORRECTION_TABLES.profile_source_aware_target_replacements.items()
}
_KOREAN_NAME_SUFFIXES = _CORRECTION_TABLES.korean_name_suffixes
_NAME_RENDERING_RULES = _CORRECTION_TABLES.name_rendering_rules
# Approval-only rules (no wrong_forms: never repair, never an obligation) may
# also match a sourced name before a copula ending ("비챤이라는", "이세돌이라고").
# Kept out of the shared suffix table: there it would activate registry aliases
# and repairs on ordinary words ("목화라는", "모카라고").
_APPROVAL_ONLY_NAME_SUFFIXES = _KOREAN_NAME_SUFFIXES | frozenset({
    "이라고", "이라는", "라고", "라는", "이야", "이지", "이고", "이라서", "이니까",
})


def _sorted_norm_items(norm: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(norm.items(), key=lambda item: len(item[0]), reverse=True))

# L4: pre-sorted normalization tables (shared, and shared+profile merged per
# profile) so the hot path doesn't rebuild + sort a dict per translation.
_SOURCE_NORM_SHARED_SORTED = _sorted_norm_items(_SOURCE_NORM_SHARED)
_SOURCE_NORM_WITH_PROFILE_SORTED = {
    profile: _sorted_norm_items({**_SOURCE_NORM_SHARED, **profile_norm})
    for profile, profile_norm in _SOURCE_NORM_BY_PROFILE.items()
}
_BOUNDARY_SOURCE_NORM_SHARED_SORTED = _sorted_norm_items(_BOUNDARY_SOURCE_NORM_SHARED)
_BOUNDARY_SOURCE_NORM_WITH_PROFILE_SORTED = {
    profile: _sorted_norm_items({**_BOUNDARY_SOURCE_NORM_SHARED, **profile_norm})
    for profile, profile_norm in _BOUNDARY_SOURCE_NORM_BY_PROFILE.items()
}


def _is_hangul_syllable(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"



def _is_name_suffix_boundary(char: str) -> bool:
    return char.isspace() or not char.isalnum()



def _source_alias_matches_at(source: str, alias: str, start: int) -> bool:
    if start > 0 and _is_hangul_syllable(source[start - 1]):
        return False

    end = start + len(alias)
    if end >= len(source):
        return True

    next_char = source[end]
    if not _is_hangul_syllable(next_char):
        return True

    suffix_end = end
    while suffix_end < len(source) and _is_hangul_syllable(source[suffix_end]):
        suffix_end += 1

    suffix = source[end:suffix_end]
    if suffix not in _KOREAN_NAME_SUFFIXES:
        return False

    return suffix_end >= len(source) or _is_name_suffix_boundary(source[suffix_end])



def _source_has_name_alias(
    source: str,
    aliases: tuple[str, ...],
    *,
    activation_policy: str = "exact_alias",
    korean_name_suffixes: frozenset[str] = _KOREAN_NAME_SUFFIXES,
) -> bool:
    for alias in aliases:
        if alias and source_alias_matches(
            source,
            alias,
            activation_policy=activation_policy,
            korean_name_suffixes=korean_name_suffixes,
        ):
            return True
    return False



def _name_rendering_rule_enabled(rule: _NameRenderingRule) -> bool:
    # Registry-backed rules activate only through the sentence retrieval result,
    # including its global collision and context checks.
    if rule.entity_id:
        return False
    if rule.scope == _SHARED_NAME_SCOPE:
        return True
    return effective_profile_applied(cfg.translation.use_profile) and effective_profile_id(
        cfg.active_streamer_profile
    ) == rule.scope



@dataclass(frozen=True)
class _EntityRequestContext:
    activations: tuple[EntityActivation, ...] = ()
    obligations: tuple[CanonicalObligation, ...] = ()
    capsule: str = ""

    @property
    def source_spans(self) -> tuple[tuple[int, int], ...]:
        return tuple(span for activation in self.activations for span in activation.source_spans)



def _resolve_entity_request_context(source: str) -> _EntityRequestContext:
    activations = ENTITY_REGISTRY.translation_mentions(
        source,
        alias_matches=lambda text, alias, start, policy: source_alias_matches_at(
            text,
            alias,
            start,
            policy,
            korean_name_suffixes=_KOREAN_NAME_SUFFIXES,
        ),
    )
    obligations: list[CanonicalObligation] = []
    capsule_rows: list[str] = []
    for activation in activations:
        entity = activation.entity
        rule = entity.translation
        assert rule is not None
        capsule_rows.append(
            f"- {activation.matched_alias} => {entity.canonical_target}"
        )
        if (
            rule.publication_policy == "required"
            and rule.condition_id == "always"
            and activation.source_spans
        ):
            obligations.append(CanonicalObligation(
                rule_id=f"entity:{entity.entity_id}",
                profile_id=(
                    rule.scope if rule.scope != _SHARED_NAME_SCOPE
                    else (entity.profile_ids[0] if entity.profile_ids else "")
                ),
                matched_alias=activation.matched_alias,
                source_spans=activation.source_spans,
                canonical_target=entity.canonical_target,
                condition_id=rule.condition_id,
            ))
    capsule = ""
    if capsule_rows:
        capsule = (
            "[Request entity mappings]\n"
            "Apply only to entities explicitly present in this source sentence. "
            "Render each source alias exactly as mapped:\n"
            + "\n".join(capsule_rows)
            + "\n[End request entity mappings]"
        )
    return _EntityRequestContext(activations, tuple(obligations), capsule)



def _canonical_obligations_for_request(
    entity_context: _EntityRequestContext,
) -> tuple[CanonicalObligation, ...]:
    return entity_context.obligations



def _request_protection_for(
    source: str,
    entity_context: _EntityRequestContext,
    obligations: tuple[CanonicalObligation, ...],
) -> RequestProtection:
    """Freeze all deterministic source ownership before provider routing."""
    resolved: list[ProtectedSourceSpan] = []
    seen: set[tuple[tuple[tuple[int, int], ...], str]] = set()
    for activation in entity_context.activations:
        key = (activation.source_spans, activation.canonical_target)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text=activation.matched_alias,
            source_spans=activation.source_spans,
            rendering=activation.canonical_target,
            rule_id=f"entity:{activation.entity.entity_id}",
        ))
    for obligation in obligations:
        key = (obligation.source_spans, obligation.canonical_target)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(ProtectedSourceSpan(
            owner="resolved_entity",
            source_text=obligation.matched_alias,
            source_spans=obligation.source_spans,
            rendering=obligation.canonical_target,
            rule_id=obligation.rule_id,
        ))
    known_source_spans = tuple(dict.fromkeys(
        span for item in resolved for span in item.source_spans
    ))
    return resolve_request_protection(
        source,
        resolved_spans=tuple(resolved),
        known_source_spans=known_source_spans,
    )



def _resolve_active_canonical_obligations(
    source: str,
) -> tuple[CanonicalObligation, ...]:
    entity_context = _resolve_entity_request_context(source)
    return _canonical_obligations_for_request(entity_context)



def _source_activated_name_canonicals(
    source: str,
    *,
    profile_id: str | None = None,
) -> frozenset[str]:
    """Existing soft name-render evidence, narrowed to this source only."""
    if profile_id is None:
        rule_enabled = _name_rendering_rule_enabled
    else:
        rule_enabled = lambda rule: rule.scope in (
            profile_id,
            _SHARED_NAME_SCOPE,
        ) and not rule.entity_id
    registry_canonicals = frozenset(
        activation.canonical_target
        for activation in _resolve_entity_request_context(source).activations
    )
    return registry_canonicals | frozenset(
        rule.canonical
        for rule in _NAME_RENDERING_RULES
        if rule_enabled(rule)
        and _source_has_name_alias(
            source,
            rule.source_aliases,
            activation_policy=(
                "name_context_required"
                if rule.repair_requires_name_context
                else "exact_alias"
            ),
            korean_name_suffixes=(
                _KOREAN_NAME_SUFFIXES
                if rule.wrong_forms
                else _APPROVAL_ONLY_NAME_SUFFIXES
            ),
        )
    )

# Per-worker record of source-normalization / target-correction rules that
# actually fired on the current translation, so the runtime event can show
# whether "海洞 -> 해둥이"-style rescues are routine or rarely needed anymore.
_LAST_CORRECTIONS = threading.local()
_CORRECTION_CAPTURE: ContextVar[list[dict] | None] = ContextVar(
    "translation_corrections_capture", default=None
)
_CORRECTION_PREVIEW: ContextVar[bool] = ContextVar(
    "translation_corrections_preview", default=False
)
_S2TWP_CONVERTER = threading.local()


def reset_corrections() -> None:
    _LAST_CORRECTIONS.value = []



def _record_correction(stage: str, rule: str, before: str, after: str) -> None:
    bucket = getattr(_LAST_CORRECTIONS, "value", None)
    if not isinstance(bucket, list):
        bucket = []
        _LAST_CORRECTIONS.value = bucket
    row = {"stage": stage, "rule": rule, "before": before, "after": after}
    bucket.append(row)
    captured = _CORRECTION_CAPTURE.get()
    if captured is not None and not _CORRECTION_PREVIEW.get():
        captured.append(dict(row))



def get_corrections() -> list[dict]:
    value = getattr(_LAST_CORRECTIONS, "value", None)
    return list(value) if isinstance(value, list) else []



def _normalize_deepseek_taiwan_script(text: str) -> str:
    """Convert provider output with a per-worker OpenCC converter."""
    converter = getattr(_S2TWP_CONVERTER, "value", None)
    if converter is None:
        converter = OpenCC("s2twp.json")
        _S2TWP_CONVERTER.value = converter
    normalized = converter.convert(text)
    if normalized != text:
        _record_correction("target_script_normalization", "opencc:s2twp", text, normalized)
    return normalized



def _replace_recording(text: str, wrong: str, right: str, *, stage: str, rule_id: str) -> str:
    """Apply text.replace(wrong, right), recording the rule iff it changed text."""
    if wrong and wrong in text:
        new = text.replace(wrong, right)
        if new != text:
            _record_correction(stage, rule_id, wrong, right)
            return new
    return text



def _replace_source_alias_recording(
    text: str,
    wrong: str,
    right: str,
    *,
    rule_id: str,
) -> str:
    """Replace a Korean source alias only at the existing name boundaries."""
    if not wrong:
        return text

    pieces: list[str] = []
    cursor = 0
    search_from = 0
    changed = False
    while True:
        start = text.find(wrong, search_from)
        if start < 0:
            break
        end = start + len(wrong)
        if _source_alias_matches_at(text, wrong, start):
            pieces.extend((text[cursor:start], right))
            cursor = end
            changed = True
            search_from = end
        else:
            search_from = start + 1

    if not changed:
        return text
    pieces.append(text[cursor:])
    corrected = "".join(pieces)
    _record_correction("source_norm", rule_id, wrong, right)
    return corrected



def _replace_wrong_name_forms(result: str, rule: _NameRenderingRule) -> str:
    if not rule.wrong_forms:
        return result

    alternatives = sorted({rule.canonical, *rule.wrong_forms}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(alternative) for alternative in alternatives))

    def replace_if_bounded(match: re.Match) -> str:
        form = match.group(0)
        start, end = match.span()
        if any(_is_hangul_syllable(char) for char in form):
            if not _source_alias_matches_at(result, form, start):
                return form
        if any(_is_latin_letter(char) for char in form):
            if start > 0 and _is_latin_letter(result[start - 1]):
                return form
            if end < len(result) and _is_latin_letter(result[end]):
                return form
        return rule.canonical

    corrected = pattern.sub(replace_if_bounded, result)
    if corrected != result:
        present = "|".join(form for form in rule.wrong_forms if form in result)
        _record_correction("name_render", f"name:{rule.canonical}", present, rule.canonical)
    return corrected



def _normalize_source_before_matching(text: str) -> str:
    """Replace known unambiguous STT noise forms with their canonical source alias.

    Runs before slang lookup, cache lookup, LLM call, and source-aware corrections.
    Operates on prepared text only; raw_text stored in TranslationOutcome is untouched.
    Profile-gated: normalization only applies when the matching profile is active.
    """
    profile_id = effective_profile_id(cfg.active_streamer_profile)
    if profile_id and effective_profile_applied(cfg.translation.use_profile):
        boundary_items = _BOUNDARY_SOURCE_NORM_WITH_PROFILE_SORTED.get(
            profile_id,
            _BOUNDARY_SOURCE_NORM_SHARED_SORTED,
        )
    else:
        boundary_items = _BOUNDARY_SOURCE_NORM_SHARED_SORTED
    for noisy, canonical in boundary_items:
        text = _replace_source_alias_recording(
            text,
            noisy,
            canonical,
            rule_id=f"boundary:{noisy}->{canonical}",
        )

    if profile_id and effective_profile_applied(cfg.translation.use_profile):
        items = _SOURCE_NORM_WITH_PROFILE_SORTED.get(profile_id, _SOURCE_NORM_SHARED_SORTED)
    else:
        items = _SOURCE_NORM_SHARED_SORTED
    for noisy, canonical in items:
        text = _replace_recording(
            text, noisy, canonical, stage="source_norm", rule_id=f"{noisy}->{canonical}"
        )

    conditional_groups = _CONDITIONAL_SOURCE_NORM_SHARED
    if profile_id and effective_profile_applied(cfg.translation.use_profile):
        conditional_groups += _CONDITIONAL_SOURCE_NORM_BY_PROFILE.get(profile_id, ())
    for source_terms, replacements, match_all in conditional_groups:
        if not _source_terms_match(text, source_terms, match_all):
            continue
        for noisy, canonical in replacements:
            text = _replace_recording(
                text,
                noisy,
                canonical,
                stage="source_norm",
                rule_id=f"conditional:{noisy}->{canonical}",
            )
    return text



def _source_terms_match(source: str, source_terms: tuple[str, ...], match_all: bool) -> bool:
    if match_all:
        return all(term in source for term in source_terms)
    return any(term in source for term in source_terms)



def _apply_source_aware_corrections(source: str, result: str) -> str:
    corrected = result
    for wrong, right in _TARGET_SCRIPT_NORMALIZATION:
        corrected = _replace_recording(
            corrected,
            wrong,
            right,
            stage="target_script_normalization",
            rule_id=f"zh-tw:{wrong}->{right}",
        )
    for source_terms, replacements, match_all in _SOURCE_AWARE_TARGET_REPLACEMENTS:
        if not _source_terms_match(source, source_terms, match_all):
            continue
        for wrong, right in replacements:
            corrected = _replace_recording(
                corrected, wrong, right, stage="target_correction", rule_id=f"{wrong}->{right}"
            )

    if effective_profile_applied(cfg.translation.use_profile):
        profile_replacements = _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS.get(
            effective_profile_id(cfg.active_streamer_profile), ()
        )
        for source_terms, replacements, match_all in profile_replacements:
            if not _source_terms_match(source, source_terms, match_all):
                continue
            for wrong, right in replacements:
                corrected = _replace_recording(
                    corrected, wrong, right,
                    stage="target_correction", rule_id=f"profile:{wrong}->{right}",
                )

    return _apply_source_gated_name_rendering(source, corrected)



def _target_has_bounded_term(target: str, term: str) -> bool:
    """Match a canonical target as a token, not as part of another name."""
    latin_target = any(_is_latin_letter(char) for char in term)
    search_target = target.casefold() if latin_target else target
    search_term = term.casefold() if latin_target else term
    start = search_target.find(search_term)
    while start >= 0:
        end = start + len(search_term)
        left = search_target[start - 1] if start else ""
        right = search_target[end] if end < len(search_target) else ""
        if not (
            left and search_term[0].isascii() and search_term[0].isalnum()
            and left.isascii() and left.isalnum()
        ) and not (
            right and search_term[-1].isascii() and search_term[-1].isalnum()
            and right.isascii() and right.isalnum()
        ) and not (
            left and _is_hangul_syllable(search_term[0]) and _is_hangul_syllable(left)
        ) and not (
            right and _is_hangul_syllable(search_term[-1]) and _is_hangul_syllable(right)
        ):
            return True
        start = search_target.find(search_term, start + 1)
    return False



@lru_cache(maxsize=4096)
def _has_simplified_mapping(char: str) -> bool:
    return (
        len(char) == 1
        and "\u3400" <= char <= "\u9fff"
        and convert_chinese(char, "zh-tw") != char
    )



def _simplified_chinese_evidence(target: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        char for char in target if _has_simplified_mapping(char)
    ))



def _unactivated_registry_targets(source: str, target: str) -> tuple[str, ...]:
    """Reject reviewed entity renderings that lack sentence-local source proof."""
    activated_ids = {
        activation.entity.entity_id
        for activation in _resolve_entity_request_context(source).activations
    }
    return tuple(
        entity.canonical_target
        for entity in ENTITY_REGISTRY.entities
        if entity.translation is not None
        and entity.entity_id not in activated_ids
        and not _target_has_bounded_term(source, entity.canonical_target)
        and _target_has_bounded_term(target, entity.canonical_target)
    )



def _apply_source_gated_name_rendering(
    source: str,
    result: str,
) -> str:
    """Apply only exact, source-proven canonical name rendering rules."""
    corrected = result
    activated_entity_ids = frozenset(
        activation.entity.entity_id
        for activation in _resolve_entity_request_context(source).activations
    )
    for rule in _NAME_RENDERING_RULES:
        if not (
            rule.entity_id in activated_entity_ids
            or _name_rendering_rule_enabled(rule)
        ):
            continue
        if not _source_has_name_alias(
            source,
            rule.source_aliases,
            activation_policy=(
                "name_context_required"
                if rule.repair_requires_name_context
                else "exact_alias"
            ),
        ):
            continue
        corrected = _replace_wrong_name_forms(corrected, rule)

    return corrected



def _dependency_marker(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    for marker in tuple(
        cfg.translation.adaptive_history_dependency_markers or ()
    ):
        if not stripped.startswith(marker):
            continue
        suffix = stripped[len(marker):]
        if _DEPENDENCY_MARKER_BOUNDARY_RE.match(suffix):
            return marker
    return ""



def _profile_preserve_as_is_terms() -> frozenset[str]:
    if not effective_profile_applied(cfg.translation.use_profile):
        return frozenset()

    profile_id = effective_profile_id(cfg.active_streamer_profile)
    if not profile_id:
        return frozenset()

    terms = set(get_translation_profile_preserve_terms(profile_id))
    for rule in _NAME_RENDERING_RULES:
        if rule.scope not in (profile_id, _SHARED_NAME_SCOPE):
            continue
        # Only the canonical spelling may pass unchanged. An alias such as
        # 솜명이 or Haedungi still needs deterministic canonicalization.
        if rule.canonical in rule.source_aliases:
            terms.add(rule.canonical)
    return frozenset(terms)



def _contains_hangul(text: str) -> bool:
    return any(_is_hangul_syllable(char) for char in text)



def _publication_approved_terms(
    profile_id: str,
    obligations: tuple[CanonicalObligation, ...] = (),
) -> frozenset[str]:
    """Terms authorized by deterministic publication policy for this sentence."""
    terms = set(_COMMON_PRESERVED_ACRONYMS)
    if profile_id:
        terms.update(
            term
            for term in get_translation_profile_preserve_terms(profile_id)
            if not _contains_hangul(term)
        )
        terms.update(
            rule.canonical
            for rule in _NAME_RENDERING_RULES
            if rule.scope in (profile_id, _SHARED_NAME_SCOPE)
            and rule.publication_policy == "repair_only"
            and not _contains_hangul(rule.canonical)
        )
    # Hangul publication approval is sentence-local and source-proven.  Do not
    # allow an unrelated active-profile name to mask ordinary Korean residue.
    terms.update(obligation.canonical_target for obligation in obligations)
    return frozenset(term for term in terms if term)



def _preview_source_aware_corrections(
    source: str,
    result: str,
    *,
    name_render_only: bool = False,
) -> tuple[str, list[dict]]:
    """Preview deterministic target fixes without contaminating selected trace."""
    sentinel = object()
    preview_token = _CORRECTION_PREVIEW.set(True)
    previous = getattr(_LAST_CORRECTIONS, "value", sentinel)
    _LAST_CORRECTIONS.value = []
    try:
        corrected = (
            _apply_source_gated_name_rendering(source, result)
            if name_render_only
            else _apply_source_aware_corrections(source, result)
        )
        corrections = get_corrections()
    finally:
        _CORRECTION_PREVIEW.reset(preview_token)
        if previous is sentinel:
            try:
                delattr(_LAST_CORRECTIONS, "value")
            except AttributeError:
                pass
        else:
            _LAST_CORRECTIONS.value = previous
    return corrected, corrections



def _is_latin_letter(char: str) -> bool:
    return bool(char) and char.isalpha() and "LATIN" in unicodedata.name(char, "")
