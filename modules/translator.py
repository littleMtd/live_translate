import hashlib
import queue
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import cfg
from modules.activity_context import (
    ActivitySnapshot,
    activity_prompt_capsule,
    activity_snapshot_metadata,
    bind_activity_snapshot,
    bind_profile_id,
    bound_activity_snapshot,
    capture_effective_activity_snapshot,
    effective_activity_value,
    effective_profile_id,
    normalize_activity,
)
from modules.profile_context import (
    ProfileSnapshot,
    bind_profile_snapshot,
    bound_profile_snapshot,
    effective_profile_applied,
    profile_state,
)
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import (
    runtime_events,
    source_proven_quality_terms,
    translation_quality,
)
from modules.pipeline_events import sentence_incomplete, sentence_metadata, sentence_text
from modules.provisional_subtitles import (
    ProvisionalCandidate,
    ProvisionalRequest,
    ProvisionalStore,
    SubtitlePayload,
    provisional_fingerprint,
)
from modules.db import _get_db
from modules.translation_prompts import (
    _BASE_PROMPT,
    _BASE_PROMPT_TAIL,
    _QWEN_PROMPT,
    _QWEN_PROMPT_TAIL,
    _is_qwen_model,
    get_translation_profile,
    get_translation_profile_output_terms,
    get_translation_profile_preserve_terms,
)
from modules.streamer_profiles import common_stt_terms
from modules.translation_engines import (
    TranslationEngine,
    DeepSeekTranslationEngine,
    _build_engine_chain,
    build_effective_deepseek_messages,
    build_effective_qwen_messages,
    effective_system_prompt_for_engine,
    engine_chain_config_key,
    get_last_engine_api_diagnostics,
    get_last_engine_diagnostics,
    get_last_token_usage,
    get_selected_token_usage,
    get_selected_translation_attempt,
    get_translation_attempts,
    record_translation_attempt,
    reset_last_engine_diagnostics,
    reset_last_token_usage,
    reset_translation_call_trace,
    translation_route_id,
)
from modules.translation_runtime import (
    FallbackState,
    active_engine,
    call_with_fallback,
    probe_primary_recovery,
)
from modules.translation_memory import HistoryCohort, MemoryLookup, TranslationMemory
from modules.translation_policy import RepetitionEvidence, TranslationPolicy
from modules.unknown_name_escrow import (
    UnknownNameEscrow,
    resolve_unknown_name_escrow,
)
from modules.semantic_terminology import (
    SemanticTerminologyEscrow,
    resolve_semantic_terminology,
)
from modules.translation_corrections import (
    CanonicalObligation,
    CanonicalObligationEvaluation,
    NameRenderingRule as _NameRenderingRule,
    evaluate_canonical_obligations,
    load_translation_corrections,
    resolve_canonical_obligations,
    source_alias_matches,
)

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_INTERVAL_SEC = 30.0
_FALLBACK_PROBE_TEXT = "안녕하세요"
_FALLBACK_THRESHOLD = 3      # consecutive primary failures before hard-switching to fallback
_LIVE_FALLBACK_THRESHOLD = 1
_TRANSLATION_WORKERS = 2
_MAX_PENDING_TRANSLATIONS = 4
_TRANSLATION_LOOP_POLL_SEC = 0.05
_MODEL_REFUSAL_RE = re.compile(
    r"^\s*(?:"
    r"(?:translation|output|target|번역|출력|译文|譯文)\s*[:：]"
    r"|(?:(?:i(?:'m| am)\s+)?sorry[,;:]?\s*(?:but\s+)?)?"
    r"i\s+(?:cannot|can't|can\s+not)\s+"
    r"(?:translate|provide\s+(?:a\s+)?translation)\b"
    r"|i\s+am\s+unable\s+to\s+"
    r"(?:translate|provide\s+(?:a\s+)?translation)\b"
    r"|as\s+an\s+ai\b"
    r"|unable\s+to\s+translate\b"
    r")",
    re.IGNORECASE,
)
_API_EVENT_DEFAULTS = {
    "api_attempt_count": 0,
    "api_timeout_count": 0,
    "api_total_wall_ms": None,
    "api_final_attempt_ms": None,
    "api_first_attempt_ms": None,
    "api_retry_attempt_ms": None,
    "retry_sleep_ms": 0.0,
    "timeout_config_ms": None,
    "api_attempt_timeout_ms": None,
    "api_attempt_index": 0,
    "api_inflight_count_at_start": None,
    "source_text_char_count": None,
    "prompt_char_count": None,
    "request_body_char_count": None,
    "message_count": None,
    "context_item_count": None,
    "api_error_type": None,
    "api_error_message_class": None,
    "api_cost_usd": None,
    "deadline_exceeded": False,
    "deadline_scope": "",
    "deadline_budget_ms": None,
}
_CACHE_HIT_STATUSES = {"memory_hit", "db_hit"}
FallbackEventSink = Callable[..., None]

_HANGUL_RATIO_THRESHOLD = 0.50  # reject result if >50 % of chars are Hangul syllables
_DEPENDENCY_MARKER_BOUNDARY_RE = re.compile(r"^[\s\.,!?~…。？！,，、:;；]|$")
_DOTTED_ACRONYM_RE = re.compile(r"^(?:[A-Za-z]\.){2,}[A-Za-z]?$")
_URL_RE = re.compile(
    r"^(?:(?:https?|ftp)://|www\.)[^\s]+$",
    flags=re.IGNORECASE,
)
_BARE_DOMAIN_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?:[/?#][^\s]*)?$"
)
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_HANDLE_RE = re.compile(r"^[@#][A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{1,127}$")
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:[.,:/-]\d+)*$")
_COMMON_PRESERVED_ACRONYMS = frozenset(
    term
    for term in common_stt_terms()
    if re.fullmatch(r"[A-Z][A-Z0-9&:+./-]{1,15}", term)
)


_META_GARBAGE_MARKERS = (
    "無法理解",
    "无法理解",
    "無明確語義",
    "无明确语义",
    "STT亂碼",
    "STT乱碼",
    "STT 垃圾",
    "亂碼",
    "乱码",
    "無意義詞",
    "无意义词",
    "無意義",
    "无意义",
    "省略",
)

from modules.translation_corrections import SHARED_NAME_SCOPE as _SHARED_NAME_SCOPE
_STELLIVE_HINA_PROFILE_ID = "stellive_hina"
_HADES_PROFILE_ID = "hades_chxxnnx"
_MWMEU_PROFILE_ID = "mwmeu"
_IRISE_PROFILE_ID = "irise"
_IRISE_MUSIC_PART_CUES = (
    "고음",
    "보컬",
    "브릿지",
    "후렴",
    "랩",
    "킬링",
    "안무",
    "녹음",
    "노래",
    "하모니",
)
_IRISE_MUSIC_PART_CUE_SUFFIXES = frozenset(
    ("가", "는", "를", "의", "에서", "에도", "도", "만", "와", "과", "로", "이", "을", "랑")
)
_IRISE_BUSINESS_PART_CUES = ("사업", "담당", "업무", "부서", "조직")
_IRISE_PART_CUE_MAX_GAP = 6
_IRISE_PART_TOKEN_RE = re.compile(
    r"(?<![가-힣])파트"
    r"(?=(?:가|는|를|의|에서|에도|도|만|랑|와|과|로|부터|까지|예요|입니다|라고(?:요)?)?"
    r"(?:$|[^가-힣]))"
)

_CORRECTION_TABLES = load_translation_corrections()
_SOURCE_NORM_SHARED = _CORRECTION_TABLES.source_norm_shared
_SOURCE_NORM_BY_PROFILE = _CORRECTION_TABLES.source_norm_by_profile
_BOUNDARY_SOURCE_NORM_SHARED = _CORRECTION_TABLES.boundary_source_norm_shared
_BOUNDARY_SOURCE_NORM_BY_PROFILE = _CORRECTION_TABLES.boundary_source_norm_by_profile
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
_CANONICAL_PUBLICATION_POLICY_VERSION = "canonical-obligations-v1"


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


def _fallback_failure_threshold() -> int:
    # Live subtitles should stop paying primary timeout cost after the first
    # miss; clip/offline mode keeps the more conservative retry-before-switch.
    if cfg.translation.translation_mode == "live":
        return _LIVE_FALLBACK_THRESHOLD
    return _FALLBACK_THRESHOLD


def _translation_circuit_breaker_enabled() -> bool:
    if cfg.translation.translation_mode != "live":
        return False
    return bool(cfg.translation.circuit_breaker_enabled)


def _translation_deadline_at() -> float | None:
    if cfg.translation.translation_mode != "live":
        return None
    return time.monotonic() + float(cfg.translation.live_total_deadline_sec)


def _emit_fallback_runtime_event(action: str, **fields) -> None:
    runtime_events.emit(
        "translation_fallback",
        action=action,
        translation_mode=cfg.translation.translation_mode,
        profile_id=effective_profile_id(cfg.active_streamer_profile),
        circuit_breaker_enabled=_translation_circuit_breaker_enabled(),
        **fields,
    )


def _db_cache_enabled() -> bool:
    """SQLite cache layer is for clip mode (replays repeat); live mode showed a
    ~0.45% hit rate so it is disabled by default (cfg.database.live_db_cache)."""
    if cfg.translation.translation_mode == "clip":
        return True
    return bool(getattr(cfg.database, "live_db_cache", False))


# Deterministic guard for instruction-echo placeholders (audit §15.4): the
# model sometimes writes the *word* for "reply empty" instead of an empty
# reply (2026-07-11 run: 17x "（留空）", 15 shipped to screen). Bracketed
# forms are never legitimate translations. Bare 留空/空白 CAN be real output
# (비워 둬 → 留空, 공백 → 空白), so bare matching is restricted to phrases
# that cannot be a genuine translation of anything.
_PLACEHOLDER_BRACKETED_RE = re.compile(
    r"^[（(\[【\s]+(?:空|留空|空白|無輸出|无输出|無內容|无内容|空字串|空字符串|"
    r"零個字元|零个字符|沒有輸出|没有输出|無翻譯|无翻译|無輸出內容)"
    r"[）)\]】\s。.!…]*$"
)
_PLACEHOLDER_BARE = frozenset({
    "無輸出", "无输出", "空字串", "空字符串", "零個字元", "零个字符",
    "沒有輸出", "没有输出", "無翻譯", "无翻译",
})
_ZERO_OUTPUT_DIRECTIVES = (
    "輸出零個字元",
    "输出零个字符",
    "輸出 0 個字元",
    "输出 0 个字符",
)


def _looks_like_placeholder_output(result: str) -> bool:
    normalized = " ".join((result or "").split())
    if not normalized:
        return False
    if _PLACEHOLDER_BRACKETED_RE.match(normalized):
        return True
    return normalized.rstrip("。.!…") in _PLACEHOLDER_BARE


def _looks_like_meta_garbage_output(result: str) -> bool:
    normalized = result.strip()
    if not normalized:
        return False
    # Placeholder echo is filtered with the same semantics as meta garbage:
    # no subtitle, no cache/history write, and deliberately NO fallback call —
    # the sources are overwhelmingly noise the model was right to refuse, so
    # a fallback engine would just subtitle the noise literally.
    if _looks_like_placeholder_output(normalized):
        return True
    has_zero_output_directive = any(
        marker in normalized for marker in _ZERO_OUTPUT_DIRECTIVES
    )
    # Older models sometimes wrapped the empty-output directive in a longer
    # explanation instead of returning only the short placeholder. Catch the
    # bracketed form, plus unbracketed forms that also carry a known meta
    # explanation marker. A legitimate technical sentence such as
    # "這個函式會輸出零個字元" remains allowed.
    if has_zero_output_directive and (
        normalized.startswith(("(", "（", "[", "【"))
        or any(marker in normalized for marker in _META_GARBAGE_MARKERS)
    ):
        return True
    if "STT" in normalized.upper() and any(marker in normalized for marker in _META_GARBAGE_MARKERS):
        return True
    if normalized.startswith(("(", "（", "[", "【")) and any(
        marker in normalized for marker in _META_GARBAGE_MARKERS
    ):
        return True
    return False


def _looks_like_model_refusal(result: str) -> bool:
    return bool(_MODEL_REFUSAL_RE.search((result or "").strip()))


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
) -> bool:
    for alias in aliases:
        if alias and source_alias_matches(
            source,
            alias,
            activation_policy=activation_policy,
            korean_name_suffixes=_KOREAN_NAME_SUFFIXES,
        ):
            return True
    return False


def _name_rendering_rule_enabled(rule: _NameRenderingRule) -> bool:
    if rule.scope == _SHARED_NAME_SCOPE:
        return True
    return effective_profile_applied(cfg.translation.use_profile) and effective_profile_id(
        cfg.active_streamer_profile
    ) == rule.scope


def _resolve_active_canonical_obligations(
    source: str,
) -> tuple[CanonicalObligation, ...]:
    profile_applied = effective_profile_applied(
        bool(getattr(cfg.translation, "use_profile", False))
    )
    profile_id = (
        effective_profile_id(getattr(cfg, "active_streamer_profile", ""))
        if profile_applied
        else ""
    )
    return resolve_canonical_obligations(
        source,
        profile_id=profile_id,
        profile_applied=profile_applied,
        rules=_NAME_RENDERING_RULES,
        korean_name_suffixes=_KOREAN_NAME_SUFFIXES,
    )


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
        )
    return frozenset(
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
        )
    )


# Per-worker record of source-normalization / target-correction rules that
# actually fired on the current translation, so the runtime event can show
# whether "海洞 -> 해둥이"-style rescues are routine or rarely needed anymore.
_LAST_CORRECTIONS = threading.local()


def reset_corrections() -> None:
    _LAST_CORRECTIONS.value = []


def _record_correction(stage: str, rule: str, before: str, after: str) -> None:
    bucket = getattr(_LAST_CORRECTIONS, "value", None)
    if not isinstance(bucket, list):
        bucket = []
        _LAST_CORRECTIONS.value = bucket
    bucket.append({"stage": stage, "rule": rule, "before": before, "after": after})


def get_corrections() -> list[dict]:
    value = getattr(_LAST_CORRECTIONS, "value", None)
    return list(value) if isinstance(value, list) else []


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


def _apply_source_gated_name_rendering(source: str, result: str) -> str:
    """Apply only exact, source-proven canonical name rendering rules."""
    corrected = result
    for rule in _NAME_RENDERING_RULES:
        if not _name_rendering_rule_enabled(rule):
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
        getattr(cfg.translation, "adaptive_history_dependency_markers", ()) or ()
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
        if previous is sentinel:
            try:
                delattr(_LAST_CORRECTIONS, "value")
            except AttributeError:
                pass
        else:
            _LAST_CORRECTIONS.value = previous
    return corrected, corrections


def _translation_output_guard(
    engine: TranslationEngine,
    result: str,
    source: str,
    *,
    obligations: tuple[CanonicalObligation, ...] | None = None,
    unknown_name_escrow: UnknownNameEscrow | None = None,
    semantic_terminology: SemanticTerminologyEscrow | None = None,
) -> dict[str, object]:
    """Enforce publication script safety plus narrow Flash-specific guards.

    Every provider is judged after deterministic corrections for unexpected
    Hangul/Kana residue. Flash additionally retains its raw-output boundary:
    only exact, source-gated ``name_render`` rules may rescue a raw script
    violation, and only when those rules alone remove all residue of that type.
    """
    if obligations is None:
        obligations = _resolve_active_canonical_obligations(source)
    if unknown_name_escrow is None:
        unknown_name_escrow = UnknownNameEscrow(source, source)
    if semantic_terminology is None:
        semantic_terminology = SemanticTerminologyEscrow(source, source)
    engine_name = str(getattr(engine, "engine_name", "") or "")
    is_deepseek = engine_name == "deepseek"
    terminology_passed, terminology_reason = (
        semantic_terminology.evaluate_provider_candidate(result)
    )
    terminology_restored = (
        semantic_terminology.restore_provider_candidate(result)
        if terminology_passed
        else result
    )
    escrow_evaluation = unknown_name_escrow.evaluate_provider_candidate(
        terminology_restored
    )
    restored_result = (
        unknown_name_escrow.restore_provider_candidate(terminology_restored)
        if escrow_evaluation.passed
        else terminology_restored
    )
    corrected, corrections = _preview_source_aware_corrections(source, restored_result)
    profile_id = (
        effective_profile_id(getattr(cfg, "active_streamer_profile", ""))
        if bool(getattr(cfg.translation, "use_profile", False))
        else ""
    )
    approved_terms = set(_publication_approved_terms(profile_id, obligations))
    # Preserve the existing generic source-proof allowance, while removing the
    # old profile-wide Hangul allowlist.  A Korean span absent from this source
    # and from its activated obligations remains a script violation.
    approved_terms.update(source_proven_quality_terms(source))
    approved_terms.update(_source_activated_name_canonicals(source))
    approved_terms.update(unknown_name_escrow.approved_hangul_terms)
    approved_terms = frozenset(approved_terms)
    obligation_evaluation = evaluate_canonical_obligations(corrected, obligations)
    raw_quality = translation_quality(
        source,
        restored_result,
        approved_terms=approved_terms,
    )
    quality = translation_quality(
        source,
        corrected,
        approved_terms=approved_terms,
    )
    raw_flags = set(raw_quality.get("quality_flags") or [])
    raw_classifications = set(
        raw_quality.get("quality_classifications") or []
    )
    flags = set(quality.get("quality_flags") or [])
    classifications = set(quality.get("quality_classifications") or [])
    raw_script_violations = set()
    if "target_has_japanese" in raw_flags:
        raw_script_violations.add("unexpected_japanese")
    if "target_has_unexpected_hangul" in raw_classifications:
        raw_script_violations.add("unexpected_hangul")
    corrected_script_violations = set()
    if "target_has_japanese" in flags:
        corrected_script_violations.add("unexpected_japanese")
    if "target_has_unexpected_hangul" in classifications:
        corrected_script_violations.add("unexpected_hangul")

    name_render_rescued: set[str] = set()
    if raw_script_violations - corrected_script_violations:
        name_rendered, name_render_corrections = _preview_source_aware_corrections(
            source,
            restored_result,
            name_render_only=True,
        )
        name_render_quality = translation_quality(
            source,
            name_rendered,
            approved_terms=approved_terms,
        )
        name_render_flags = set(name_render_quality.get("quality_flags") or [])
        name_render_classifications = set(
            name_render_quality.get("quality_classifications") or []
        )
        name_render_violations = set()
        if "target_has_japanese" in name_render_flags:
            name_render_violations.add("unexpected_japanese")
        if "target_has_unexpected_hangul" in name_render_classifications:
            name_render_violations.add("unexpected_hangul")
        if any(
            correction.get("stage") == "name_render"
            for correction in name_render_corrections
        ):
            name_render_rescued = (
                raw_script_violations
                - corrected_script_violations
                - name_render_violations
            )

    reason = ""
    if not terminology_passed:
        reason = terminology_reason
    elif not escrow_evaluation.passed:
        reason = escrow_evaluation.reason
    elif not obligation_evaluation.passed:
        reason = "canonical_obligation_missing"
    elif "unexpected_japanese" in corrected_script_violations:
        reason = "unexpected_japanese"
    elif "unexpected_hangul" in corrected_script_violations:
        reason = "unexpected_hangul"
    elif is_deepseek and "unexpected_japanese" in (
        raw_script_violations - name_render_rescued
    ):
        reason = "unexpected_japanese"
    elif is_deepseek and "unexpected_hangul" in (
        raw_script_violations - name_render_rescued
    ):
        reason = "unexpected_hangul"
    elif is_deepseek and _looks_like_meta_garbage_output(corrected):
        reason = "meta_garbage_output"
    elif is_deepseek and _looks_like_model_refusal(corrected):
        reason = "model_refusal"
    elif is_deepseek and "target_meta_leak" in flags:
        reason = "target_meta_leak"
    elif is_deepseek and "repetitive_target" in flags:
        reason = "repetitive_target"
    evidence = {
        "version": 1,
        "candidate_raw_output": result,
        "candidate_output": corrected,
        "candidate_corrections": corrections,
        "candidate_quality_flags": sorted(flags),
        "candidate_quality_classifications": sorted(classifications),
        "candidate_raw_quality_flags": sorted(raw_flags),
        "candidate_raw_quality_classifications": sorted(raw_classifications),
        "canonical_obligations": obligation_evaluation.as_dict(),
        "semantic_terminology": {
            "active": semantic_terminology.active,
            "rule_ids": [term.rule_id for term in semantic_terminology.terms],
            "passed": terminology_passed,
        },
        "unknown_name_escrow": {
            "active": unknown_name_escrow.active,
            "expected": list(escrow_evaluation.expected),
            "missing": list(escrow_evaluation.missing),
            "duplicated": list(escrow_evaluation.duplicated),
            "mutated_placeholder": escrow_evaluation.mutated_placeholder,
            "invented_aliases": list(escrow_evaluation.invented_aliases),
            "approved_hangul_terms": list(
                unknown_name_escrow.approved_hangul_terms
            ),
        },
    }
    if reason:
        evidence["reason"] = reason
    elif name_render_rescued:
        evidence["accepted_after_name_render"] = sorted(name_render_rescued)
    elif not is_deepseek:
        return {}
    return evidence


def _quality_telemetry_approved_terms(
    profile_id: str,
    source_text: str,
    obligations: tuple[CanonicalObligation, ...] = (),
) -> frozenset[str]:
    """Add diagnostic-only evidence without changing publication policy."""
    terms = set(_publication_approved_terms(profile_id, obligations))
    if profile_id:
        terms.update(
            term
            for term in get_translation_profile_output_terms(profile_id)
            if not _contains_hangul(term)
        )
    terms.update(source_proven_quality_terms(source_text))
    if profile_id:
        terms.update(
            _source_activated_name_canonicals(
                source_text,
                profile_id=profile_id,
            )
        )
    return frozenset(term for term in terms if term)


def _final_script_rejection_reason(event_fields: dict[str, object]) -> str:
    """Return the authoritative publication-time script violation, if any."""
    classifications = set(event_fields.get("quality_classifications") or ())
    flags = set(event_fields.get("quality_flags") or ())
    if "target_has_unexpected_hangul" in classifications:
        return "unexpected_hangul"
    if "target_has_japanese" in flags:
        return "unexpected_japanese"
    return ""


def _is_latin_letter(char: str) -> bool:
    return bool(char) and char.isalpha() and "LATIN" in unicodedata.name(char, "")


def _bounded_korean_term_spans(
    text: str,
    terms: tuple[str, ...],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in terms:
        search_from = 0
        while True:
            start = text.find(term, search_from)
            if start < 0:
                break
            end = start + len(term)
            search_from = start + 1
            if start > 0 and _is_hangul_syllable(text[start - 1]):
                continue
            if end < len(text) and _is_hangul_syllable(text[end]):
                suffix_end = end
                while suffix_end < len(text) and _is_hangul_syllable(text[suffix_end]):
                    suffix_end += 1
                if text[end:suffix_end] not in _IRISE_MUSIC_PART_CUE_SUFFIXES:
                    continue
            spans.append((start, end))
    return spans


def _irise_music_part_semantic_candidate(source_text: str, target_text: str) -> bool:
    """Conservatively flag music/performance `파트` rendered as `部門`.

    This is diagnostic-only.  The local cue requirement intentionally leaves
    ambiguous business/team uses alone, and the token matcher excludes words
    such as `파트너`.
    """
    if "部門" not in target_text:
        return False

    for clause in re.split(r"[.!?。！？\n]+", source_text or ""):
        part_matches = list(_IRISE_PART_TOKEN_RE.finditer(clause))
        if not part_matches:
            continue
        if "저희 파트" in clause and "수정" in clause:
            return True
        cue_spans = _bounded_korean_term_spans(clause, _IRISE_MUSIC_PART_CUES)
        for part_match in part_matches:
            local_start = max(0, part_match.start() - _IRISE_PART_CUE_MAX_GAP)
            local_end = min(len(clause), part_match.end() + _IRISE_PART_CUE_MAX_GAP)
            local_text = clause[local_start:local_end]
            if any(cue in local_text for cue in _IRISE_BUSINESS_PART_CUES):
                continue
            for cue_start, cue_end in cue_spans:
                gap = max(
                    part_match.start() - cue_end,
                    cue_start - part_match.end(),
                    0,
                )
                if gap <= _IRISE_PART_CUE_MAX_GAP:
                    return True
    return False


def _profile_translation_qa(
    *,
    source_text: str,
    target_text: str | None,
    status: str,
    incomplete: bool,
    profile_id: str,
    profile_applied: bool,
    metadata: dict,
    quality: dict,
    obligation_evaluation: CanonicalObligationEvaluation | None = None,
) -> dict:
    """Return profile-scoped QA evidence without changing retry or routing.

    Deterministic repairs remain attributable through the existing
    `corrections` trace.  This helper only adds post-correction obligations and
    diagnostic suspicions; it never mutates legacy quality flags or scores.
    """
    # Canonical QA and hard publication acceptance share one resolver.  Keep
    # the legacy flat fields while avoiding a second profile-specific mapping.
    expected_terms = list(
        obligation_evaluation.expected if obligation_evaluation is not None else ()
    )

    eligible = status == "success" and not incomplete and target_text is not None
    target = target_text or ""
    missing_terms = list(
        obligation_evaluation.missing
        if eligible and obligation_evaluation is not None
        else ()
    )

    semantic_candidates: list[str] = []
    if (
        eligible
        and profile_applied
        and profile_id == _IRISE_PROFILE_ID
        and _irise_music_part_semantic_candidate(source_text, target)
    ):
        semantic_candidates.append("music_part_rendered_as_department")

    qa_flags: list[str] = []
    classifications = list(quality.get("quality_classifications") or [])
    if missing_terms:
        qa_flags.append("target_missing_profile_canonical")
        classifications.append("target_missing_profile_canonical")
    if semantic_candidates:
        qa_flags.append("target_profile_semantic_candidate")
        classifications.append("target_profile_semantic_candidate")

    # Script drift already has mature generic evidence in translation_quality;
    # mirror it only in the disposition so this profile QA remains additive.
    has_script_suspicion = (
        "target_has_japanese" in (quality.get("quality_flags") or [])
        or "target_has_unexpected_hangul" in classifications
    )
    name_render_fixed = any(
        isinstance(correction, dict)
        and correction.get("stage") == "name_render"
        and correction.get("after") in expected_terms
        for correction in (metadata.get("corrections") or [])
    )
    if qa_flags or has_script_suspicion:
        disposition = "suspicious"
    elif name_render_fixed:
        disposition = "normalized"
    else:
        disposition = "clean"

    return {
        "target_expected_canonical_terms": expected_terms,
        "target_missing_canonical_terms": missing_terms,
        "target_profile_semantic_candidates": semantic_candidates,
        "translation_qa_flags": qa_flags,
        "translation_qa_disposition": disposition,
        "quality_classifications": list(dict.fromkeys(classifications)),
    }


def _is_legitimate_preserve_as_is(source: str) -> bool:
    """Return True only when an identical translation is provably intentional."""
    text = (source or "").strip()
    if not text or "\n" in text or "\r" in text:
        return False

    if text in _profile_preserve_as_is_terms():
        return True
    if text in _COMMON_PRESERVED_ACRONYMS:
        return True
    if _DOTTED_ACRONYM_RE.fullmatch(text):
        return True
    if (
        _URL_RE.fullmatch(text)
        or _BARE_DOMAIN_RE.fullmatch(text)
        or _EMAIL_RE.fullmatch(text)
        or _HANDLE_RE.fullmatch(text)
    ):
        return True
    if _NUMERIC_LITERAL_RE.fullmatch(text):
        return True
    if _MACHINE_ID_RE.fullmatch(text):
        has_letter = any(char.isascii() and char.isalpha() for char in text)
        has_digit = any(char.isdigit() for char in text)
        if "_" in text or has_letter and has_digit:
            return True
    return False


def _looks_untranslated(result: str, source: str) -> bool:
    if result.strip() == source.strip():
        return not _is_legitimate_preserve_as_is(source)
    chars = [c for c in result if not c.isspace()]
    if not chars:
        return False
    if len(chars) < 6:
        return False  # too short for ratio to be meaningful (single Korean name is OK)
    hangul = sum(1 for c in chars if "가" <= c <= "힣")
    if (hangul / len(chars)) > _HANGUL_RATIO_THRESHOLD:
        return True
    # Japanese hiragana/katakana should never appear in zh-TW output
    japanese = sum(1 for c in chars if "぀" <= c <= "ゟ" or "゠" <= c <= "ヿ")
    if japanese > 2:
        return True
    # Result much longer than source likely means hallucinated continuation
    src_chars = len([c for c in source if not c.isspace()])
    if len(chars) > src_chars * 3 and len(chars) > 40:
        return True
    return False


_HISTORY_WRITE_LOCK = threading.Lock()


def _write_history(ko: str, zh: str) -> None:
    path = _LOG_DIR / f"translations_{datetime.now().strftime('%Y%m%d')}.txt"
    ts = datetime.now().strftime("%H:%M:%S")
    # Serialize appends: history is written outside the shared state lock (M1),
    # so two workers may reach this concurrently.
    with _HISTORY_WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {ko}\n        → {zh}\n")


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TranslationOutcome:
    source_text: str
    target_text: str | None
    status: str
    result_source: str
    cache_status: str
    incomplete: bool
    engine: str = ""
    model: str = ""
    prompt_version: str = ""
    filter_reason: str = ""
    canonical_obligation_evaluation: CanonicalObligationEvaluation | None = None
    unknown_name_approved_terms: tuple[str, ...] = ()
    deferred_success: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def route_id(self) -> str:
        provider = str(self.engine or "").strip().lower()
        model = str(self.model or "").strip()
        if not provider:
            return ""
        return f"{provider}:{model}" if model else provider

    def as_event_fields(self, latency_ms: float, metadata: dict) -> dict:
        profile_id = str(metadata.get("profile_id") or "")
        profile_applied = bool(metadata.get("profile_applied"))
        obligation_evaluation = self.canonical_obligation_evaluation
        if obligation_evaluation is None:
            obligations = resolve_canonical_obligations(
                self.source_text,
                profile_id=profile_id,
                profile_applied=profile_applied,
                rules=_NAME_RENDERING_RULES,
                korean_name_suffixes=_KOREAN_NAME_SUFFIXES,
            )
            obligation_evaluation = evaluate_canonical_obligations(
                self.target_text, obligations
            )
        approved_terms = _quality_telemetry_approved_terms(
            profile_id if profile_applied else "",
            self.source_text,
            (
                obligation_evaluation.obligations
            ),
        )
        approved_terms = frozenset(
            set(approved_terms) | set(self.unknown_name_approved_terms)
        )
        quality = translation_quality(
            self.source_text,
            self.target_text,
            approved_terms=approved_terms,
        )
        profile_qa = _profile_translation_qa(
            source_text=self.source_text,
            target_text=self.target_text,
            status=self.status,
            incomplete=self.incomplete,
            profile_id=profile_id,
            profile_applied=profile_applied,
            metadata=metadata,
            quality=quality,
            obligation_evaluation=obligation_evaluation,
        )
        obligation_fields = {
            "canonical_publication_policy_version": (
                _CANONICAL_PUBLICATION_POLICY_VERSION
            ),
            "canonical_obligation_evaluation": obligation_evaluation.as_dict(),
        }
        return {
            "source_text": self.source_text,
            "target_text": self.target_text,
            "status": self.status,
            "result_source": self.result_source,
            "cache_status": self.cache_status,
            "incomplete": self.incomplete,
            "engine": self.engine,
            "model": self.model,
            "route_id": self.route_id,
            "prompt_version": self.prompt_version,
            "filter_reason": self.filter_reason,
            "target_unknown_name_escrow_terms": list(
                self.unknown_name_approved_terms
            ),
            "latency_ms": round(latency_ms, 2),
            **metadata,
            **quality,
            **profile_qa,
            **obligation_fields,
        }


@dataclass
class _TranslatorSharedState:
    memory: TranslationMemory
    policy: TranslationPolicy
    fallback: FallbackState
    lock: object
    fallback_event_sink: FallbackEventSink | None = None
    inflight_sources: dict[str, int] = field(default_factory=dict)
    history_session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


def _new_translation_memory() -> TranslationMemory:
    try:
        recent_window = int(getattr(cfg.translation, "context_window", 0) or 0)
    except (TypeError, ValueError):
        recent_window = 0
    recent_window = max(recent_window, 0)
    return TranslationMemory(
        recent_window=recent_window,
        max_cache_size=_CACHE_MAX_SIZE,
        db_factory=_get_db,
        history_writer=_write_history,
    )


def _new_translation_policy() -> TranslationPolicy:
    return TranslationPolicy(
        slang=cfg.translation.slang,
        min_translate_chars=_MIN_TRANSLATE_CHARS,
        max_translate_chars=cfg.translation.max_translate_chars,
        repetition_confidence_exempt_enabled=(
            cfg.translation.repetition_confidence_exempt_enabled
        ),
        repetition_avg_logprob_threshold=cfg.stt.context_avg_logprob_threshold,
        repetition_no_speech_threshold=cfg.stt.context_no_speech_threshold,
    )


def _history_cohort_for(
    snapshot: ActivitySnapshot | None,
    profile_id: object,
) -> HistoryCohort:
    return (
        str(profile_id or "").strip() or "default",
        (snapshot.activity_id if snapshot is not None else "") or "unknown",
        max(0, int(snapshot.cohort_epoch or 0)) if snapshot is not None else 0,
    )


def _new_translator_shared_state(
    *,
    fallback_event_sink: FallbackEventSink | None = None,
) -> _TranslatorSharedState:
    return _TranslatorSharedState(
        memory=_new_translation_memory(),
        policy=_new_translation_policy(),
        fallback=FallbackState(),
        lock=threading.RLock(),
        fallback_event_sink=fallback_event_sink,
    )


def _copy_fallback_state(state: FallbackState) -> FallbackState:
    return FallbackState(
        state.active_idx,
        state.consecutive_primary_failures,
        state.primary_cooldown_until,
        state.consecutive_probe_successes,
    )


def _send_fallback_event(
    shared_state: _TranslatorSharedState | None,
    action: str,
    **fields,
) -> None:
    if shared_state is None:
        return
    sink = shared_state.fallback_event_sink
    if sink is None:
        return
    try:
        sink(action, **fields)
    except Exception:
        metrics.increment("translation.fallback.runtime_event_error")
        log.exception("Failed to persist translation fallback event")


def _merge_fallback_state(shared: FallbackState, before: FallbackState, after: FallbackState) -> None:
    if (
        shared.active_idx == before.active_idx
        and shared.consecutive_primary_failures == before.consecutive_primary_failures
        and shared.primary_cooldown_until == before.primary_cooldown_until
        and shared.consecutive_probe_successes == before.consecutive_probe_successes
    ):
        shared.active_idx = after.active_idx
        shared.consecutive_primary_failures = after.consecutive_primary_failures
        shared.primary_cooldown_until = after.primary_cooldown_until
        shared.consecutive_probe_successes = after.consecutive_probe_successes
        return

    if (
        after.active_idx == 0
        and before.active_idx > 0
        and shared.active_idx == before.active_idx
        and shared.primary_cooldown_until == before.primary_cooldown_until
        and shared.consecutive_probe_successes == before.consecutive_probe_successes
    ):
        shared.active_idx = 0
        shared.consecutive_primary_failures = 0
        shared.primary_cooldown_until = 0.0
        shared.consecutive_probe_successes = 0
        return

    if (
        shared.active_idx == before.active_idx
        and after.active_idx > before.active_idx
        and after.active_idx >= shared.active_idx
    ):
        previous_cooldown_until = shared.primary_cooldown_until
        shared.active_idx = after.active_idx
        shared.consecutive_primary_failures = after.consecutive_primary_failures
        shared.primary_cooldown_until = max(previous_cooldown_until, after.primary_cooldown_until)
        if after.primary_cooldown_until >= previous_cooldown_until:
            shared.consecutive_probe_successes = after.consecutive_probe_successes
        return

    if shared.active_idx == after.active_idx:
        shared.consecutive_primary_failures = max(
            shared.consecutive_primary_failures,
            after.consecutive_primary_failures,
        )
        if after.primary_cooldown_until > shared.primary_cooldown_until:
            shared.primary_cooldown_until = after.primary_cooldown_until
            shared.consecutive_probe_successes = after.consecutive_probe_successes
        elif after.primary_cooldown_until == shared.primary_cooldown_until:
            shared.consecutive_probe_successes = max(
                shared.consecutive_probe_successes,
                after.consecutive_probe_successes,
            )


def _outcome_used_api(outcome: TranslationOutcome) -> bool:
    if outcome.result_source == "api":
        return True
    if outcome.status == "failed" and outcome.result_source == "none":
        return True
    if outcome.result_source == "post_policy" and outcome.cache_status not in _CACHE_HIT_STATUSES:
        return True
    return False


def _api_event_fields(
    outcome: TranslationOutcome,
    diagnostics: dict[str, int | float | str | None],
) -> dict:
    fields = dict(_API_EVENT_DEFAULTS)
    engine = str(diagnostics.get("engine") or "")
    diagnostic_route = str(diagnostics.get("route_id") or "")
    if (
        not engine
        or engine != outcome.engine
        or (
            diagnostic_route
            and diagnostic_route != outcome.route_id
        )
        or not _outcome_used_api(outcome)
    ):
        return fields
    if int(diagnostics.get("api_attempt_count") or 0) <= 0:
        return fields
    for key in fields:
        fields[key] = diagnostics.get(key, fields[key])
    return fields


def _retry_diagnostics_apply(outcome: TranslationOutcome, diagnostics: dict[str, int | str]) -> bool:
    engine = str(diagnostics.get("engine") or "")
    diagnostic_route = str(diagnostics.get("route_id") or "")
    return bool(
        engine
        and engine == outcome.engine
        and (not diagnostic_route or diagnostic_route == outcome.route_id)
        and _outcome_used_api(outcome)
    )


def _token_usage_for_outcome(outcome: TranslationOutcome) -> dict[str, int | None]:
    if not _outcome_used_api(outcome):
        return {}
    selected = get_selected_translation_attempt()
    selected_route = str(selected.get("route_id") or "")
    if selected_route:
        if selected_route != outcome.route_id:
            return {}
    elif str(selected.get("engine") or "").strip().lower() != str(outcome.engine or "").strip().lower():
        return {}
    return get_selected_token_usage()


@dataclass(frozen=True)
class _CompletedTranslation:
    seq: int
    outcome: TranslationOutcome
    elapsed: float
    metadata: dict
    submitted_at: float
    started_at: float
    completed_at: float
    worker_id: str
    retry_count: int
    retry_reason: str
    api_event_fields: dict
    policy_input: str = ""


class _DaemonWorkerPool:
    """Small fixed-size pool whose workers cannot keep the process alive.

    ``ThreadPoolExecutor`` uses non-daemon threads and registers them for an
    interpreter-exit join.  A provider call stuck below Python's timeout layer
    could therefore outlive the bounded translator drain and prevent process
    exit.  This pool keeps the same ``Future`` interface used by the coordinator
    while making the worker lifetime subordinate to the pipeline process.
    """

    def __init__(self, max_workers: int, *, thread_name_prefix: str):
        self._tasks: queue.Queue[tuple[Future, Callable, tuple, dict] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            start_daemon_thread(f"{thread_name_prefix}_{index}", self._worker)
            for index in range(max_workers)
        ]

    def _worker(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            future, fn, args, kwargs = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def submit(self, fn: Callable, /, *args, **kwargs) -> Future:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: Future = Future()
            self._tasks.put((future, fn, args, kwargs))
            return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        task = self._tasks.get_nowait()
                    except queue.Empty:
                        break
                    if task is not None:
                        task[0].cancel()
            for _thread in self._threads:
                self._tasks.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


class Translator:
    def __init__(
        self,
        shared_state: _TranslatorSharedState | None = None,
        *,
        defer_success_record: bool = False,
    ):
        self._shared_state = shared_state or _new_translator_shared_state()
        self._state_lock = self._shared_state.lock
        self._fallback_state_obj = self._shared_state.fallback
        self._engines: list[TranslationEngine] = _build_engine_chain()
        self._engines_key = engine_chain_config_key()
        self._memory = self._shared_state.memory
        self._policy = self._shared_state.policy
        self._last_input: str = ""
        self._defer_success_record = defer_success_record
        self._history_session_id = self._shared_state.history_session_id
        self._last_provisional_trace: dict = {}

    def _state_guard(self):
        return getattr(self, "_state_lock", None) or nullcontext()

    def _fallback_state(self) -> FallbackState:
        state = getattr(self, "_fallback_state_obj", None)
        if state is None:
            state = FallbackState()
            self._fallback_state_obj = state
        return state

    def _refresh_engines_if_needed(self) -> None:
        current_key = engine_chain_config_key()
        previous_key = getattr(self, "_engines_key", current_key)
        if previous_key == current_key:
            self._engines_key = current_key
            return
        with self._state_guard():
            if getattr(self, "_engines_key", previous_key) == current_key:
                return
            self._engines = _build_engine_chain()
            self._engines_key = current_key
            state = self._fallback_state()
            state.active_idx = 0
            state.consecutive_primary_failures = 0
            state.primary_cooldown_until = 0.0
            state.consecutive_probe_successes = 0

    @property
    def _active_idx(self) -> int:
        return self._fallback_state().active_idx

    @_active_idx.setter
    def _active_idx(self, value: int) -> None:
        self._fallback_state().active_idx = value

    @property
    def _consecutive_primary_failures(self) -> int:
        return self._fallback_state().consecutive_primary_failures

    @_consecutive_primary_failures.setter
    def _consecutive_primary_failures(self, value: int) -> None:
        self._fallback_state().consecutive_primary_failures = value

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        return self.translate_event(text, incomplete).target_text

    def translate_event(
        self,
        text: str,
        incomplete: bool = False,
        *,
        repetition_evidence: RepetitionEvidence | None = None,
        provisional_candidate: ProvisionalCandidate | None = None,
        source_utterance_ids: tuple[str, ...] = (),
        evidence_source_utterance_ids: tuple[str, ...] = (),
    ) -> TranslationOutcome:
        snapshot = bound_activity_snapshot() or capture_effective_activity_snapshot(
            getattr(cfg.translation, "current_activity", ""),
            automatic_enabled=bool(
                getattr(cfg.scene, "publish_translation_activity", False)
            ),
            source_text=text,
        )
        with bind_activity_snapshot(snapshot):
            return self._translate_event_with_snapshot(
                text,
                incomplete,
                repetition_evidence=repetition_evidence,
                provisional_candidate=provisional_candidate,
                source_utterance_ids=source_utterance_ids,
                evidence_source_utterance_ids=evidence_source_utterance_ids,
            )

    def _translate_event_with_snapshot(
        self,
        text: str,
        incomplete: bool = False,
        *,
        repetition_evidence: RepetitionEvidence | None = None,
        provisional_candidate: ProvisionalCandidate | None = None,
        source_utterance_ids: tuple[str, ...] = (),
        evidence_source_utterance_ids: tuple[str, ...] = (),
    ) -> TranslationOutcome:
        raw_text = (text or "").strip()
        self._last_provisional_trace = {}
        history_cohort = self._history_cohort()
        if repetition_evidence is not None:
            # The call argument is authoritative.  A stale/malformed evidence
            # object must never relabel an incomplete sentence as complete.
            repetition_evidence = RepetitionEvidence(
                min_avg_logprob=repetition_evidence.min_avg_logprob,
                max_no_speech_prob=repetition_evidence.max_no_speech_prob,
                cut_reason=repetition_evidence.cut_reason,
                forced=repetition_evidence.forced,
                incomplete=incomplete,
            )
        policy = self._policy_state()
        # L1: rejection_reason, prepare_input and the sanitize-rejection read
        # must share one lock section — the policy instance is shared across
        # workers, so split reads raced and produced wrong filter_reason values.
        with self._state_guard():
            filter_reason = policy.rejection_reason(
                raw_text,
                repetition_evidence=repetition_evidence,
            )
            text = policy.prepare_input(
                raw_text,
                initial_rejection_reason=filter_reason,
                repetition_evidence=repetition_evidence,
            )
            self._last_input = policy.last_input
            sanitize_rejection = policy._last_sanitize_rejection
        if text is None:
            return TranslationOutcome(
                source_text=raw_text,
                target_text=None,
                status="filtered",
                result_source="policy",
                cache_status="skipped",
                incomplete=incomplete,
                filter_reason=filter_reason or sanitize_rejection or "unknown",
            )

        text = _normalize_source_before_matching(text)
        canonical_obligations = _resolve_active_canonical_obligations(text)
        known_source_spans = tuple(
            span
            for obligation in canonical_obligations
            for span in obligation.source_spans
        )
        unknown_name_escrow = resolve_unknown_name_escrow(
            text,
            known_source_spans=known_source_spans,
        )
        semantic_terminology = resolve_semantic_terminology(
            unknown_name_escrow.provider_source
        )
        provider_text = semantic_terminology.provider_source

        # Direct paths predate escrow and cannot preserve its provider token.
        # Keep them unavailable for the narrowly escrowed sentences rather than
        # allowing a cache/slang result to bypass the final identity invariant.
        slang_result = (
            None
            if unknown_name_escrow.active or semantic_terminology.active
            else self._translate_slang(text, incomplete)
        )
        if slang_result:
            slang_preview, _ = _preview_source_aware_corrections(text, slang_result)
            slang_evaluation = evaluate_canonical_obligations(
                slang_preview, canonical_obligations
            )
            if slang_evaluation.passed:
                slang_result = _apply_source_aware_corrections(text, slang_result)
                success_commit = lambda: self._record_direct_success(
                    text,
                    slang_result,
                    incomplete,
                    history_cohort,
                )
                if not getattr(self, "_defer_success_record", False):
                    success_commit()
                    success_commit = None
                return TranslationOutcome(
                    source_text=raw_text,
                    target_text=slang_result,
                    status="success",
                    result_source="slang",
                    cache_status="skipped",
                    incomplete=incomplete,
                    canonical_obligation_evaluation=slang_evaluation,
                    deferred_success=success_commit,
                )
            metrics.increment("translation.canonical_obligation.slang_rejected")

        # 根据当前模型选择对应的 prompt
        self._refresh_engines_if_needed()
        system_prompt = self._build_system_prompt()
        engine = self._active_engine()
        prompt_ver = self._prompt_version_for_engine(engine, system_prompt)
        self._log_prompt_mode_once()

        lookup = (
            MemoryLookup(None, "miss")
            if unknown_name_escrow.active or semantic_terminology.active
            else self._lookup_existing_translation_event(
                text, incomplete, prompt_ver, engine, history_cohort
            )
        )
        if lookup.result:
            target_preview, _ = _preview_source_aware_corrections(text, lookup.result)
            cache_evaluation = evaluate_canonical_obligations(
                target_preview, canonical_obligations
            )
            if not cache_evaluation.passed:
                metrics.increment("translation.canonical_obligation.cache_rejected")
                self._invalidate_cached_translation(
                    text, incomplete, prompt_ver, engine, lookup.result
                )
                lookup = MemoryLookup(None, "miss")
            else:
                target_text = _apply_source_aware_corrections(text, lookup.result)
        if lookup.result:
            if (
                _looks_like_meta_garbage_output(target_text)
                or _looks_like_model_refusal(target_text)
            ):
                self._invalidate_cached_translation(text, incomplete, prompt_ver, engine, lookup.result)
                self._reset_failed_input()
                return TranslationOutcome(
                    source_text=raw_text,
                    target_text=None,
                    status="filtered",
                    result_source="post_policy",
                    cache_status=lookup.source,
                    incomplete=incomplete,
                    filter_reason="meta_garbage_output",
                    engine=engine.engine_name if engine else "",
                    model=engine.model_name if engine else "",
                    prompt_version=prompt_ver,
                )
            success_commit = None
            if getattr(self, "_defer_success_record", False):
                success_commit = lambda: self._record_lookup_context(
                    text,
                    target_text,
                    incomplete,
                    history_cohort,
                )
            return TranslationOutcome(
                source_text=raw_text,
                target_text=target_text,
                status="success",
                result_source=lookup.source,
                cache_status=lookup.source,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_ver,
                canonical_obligation_evaluation=cache_evaluation,
                deferred_success=success_commit,
            )

        with self._state_guard():
            history = self._memory_state().context(history_cohort)
        frozen_messages_by_engine = {
            "deepseek": build_effective_deepseek_messages(
                provider_text, system_prompt, incomplete, history
            ),
            "openrouter": build_effective_qwen_messages(
                provider_text, system_prompt, incomplete, history
            ),
        }
        effective_deepseek_messages = frozen_messages_by_engine["deepseek"]
        deadline_at = _translation_deadline_at()
        promoted = False
        result = None
        used_engine = None
        if provisional_candidate is not None:
            snapshot = bound_activity_snapshot()
            profile_snapshot = bound_profile_snapshot()
            assert snapshot is not None
            fingerprint = provisional_fingerprint(
                prepared_source=text,
                source_utterance_ids=source_utterance_ids,
                evidence_source_utterance_ids=evidence_source_utterance_ids,
                profile_id=(
                    profile_snapshot.effective_profile_id
                    if profile_snapshot is not None else history_cohort[0]
                ),
                profile_cache_identity=(
                    profile_snapshot.cache_identity
                    if profile_snapshot is not None else ""
                ),
                activity_cache_identity=snapshot.cache_identity,
                history_cohort=history_cohort,
                messages=effective_deepseek_messages,
                incomplete=incomplete,
            )
            if fingerprint == provisional_candidate.fingerprint:
                provisional_engine = DeepSeekTranslationEngine()
                guard = _translation_output_guard(
                    provisional_engine,
                    provisional_candidate.raw_target,
                    text,
                    obligations=canonical_obligations,
                    unknown_name_escrow=unknown_name_escrow,
                    semantic_terminology=semantic_terminology,
                )
                if not guard.get("reason"):
                    result = str(
                        guard.get("candidate_output")
                        or provisional_candidate.raw_target
                    )
                    used_engine = provisional_engine
                    promoted = True
                    self._last_provisional_trace = {
                        "promotion_attempted": True,
                        "promotion_passed": True,
                        "fingerprint_match": True,
                    }
                else:
                    self._last_provisional_trace = {
                        "promotion_attempted": True,
                        "promotion_passed": False,
                        "fingerprint_match": True,
                        "guard_rejection": str(guard.get("reason") or ""),
                        "final_retranslation": True,
                    }
            else:
                self._last_provisional_trace = {
                    "promotion_attempted": True,
                    "promotion_passed": False,
                    "fingerprint_match": False,
                    "fingerprint_mismatch": True,
                    "final_retranslation": True,
                }
        if not promoted:
            result, used_engine = self._call_with_fallback(
                provider_text,
                system_prompt,
                incomplete,
                history,
                deadline_at=deadline_at,
                frozen_messages_by_engine=frozen_messages_by_engine,
                canonical_obligations=canonical_obligations,
                source_text=text,
                unknown_name_escrow=unknown_name_escrow,
                semantic_terminology=semantic_terminology,
            )
        # Attribute the outcome to the engine that actually produced it: on a
        # soft fallback the active engine stays primary, so reading
        # _active_engine() here mislabeled the result source, API diagnostics
        # and the DB cache row.
        engine = used_engine or self._active_engine()
        prompt_ver = self._prompt_version_for_engine(engine, system_prompt)
        return self._finalize_translation_result(
            raw_text=raw_text,
            prepared_text=text,
            provider_result=result,
            promoted=promoted,
            engine=engine,
            prompt_version=prompt_ver,
            cache_status=lookup.source,
            incomplete=incomplete,
            canonical_obligations=canonical_obligations,
            unknown_name_escrow=unknown_name_escrow,
            semantic_terminology=semantic_terminology,
            history_cohort=history_cohort,
        )

    def _reset_failed_input(self) -> None:
        """Release duplicate suppression after a retryable final failure."""
        with self._state_guard():
            policy = self._policy_state()
            if policy.last_input == self._last_input:
                policy.reset_last_input()
            self._last_input = ""

    def _finalize_translation_result(
        self,
        *,
        raw_text: str,
        prepared_text: str,
        provider_result: str | None,
        promoted: bool,
        engine: TranslationEngine | None,
        prompt_version: str,
        cache_status: str,
        incomplete: bool,
        canonical_obligations: tuple[CanonicalObligation, ...],
        unknown_name_escrow: UnknownNameEscrow,
        semantic_terminology: SemanticTerminologyEscrow,
        history_cohort: HistoryCohort,
    ) -> TranslationOutcome:
        """Sole finalizer for primary, fallback, and provisional candidates."""
        if not provider_result:
            self._reset_failed_input()
            return TranslationOutcome(
                source_text=raw_text,
                target_text=None,
                status="failed",
                result_source="none",
                cache_status=cache_status,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_version,
                canonical_obligation_evaluation=evaluate_canonical_obligations(
                    None, canonical_obligations
                ),
            )

        result = provider_result
        if not promoted:
            result = unknown_name_escrow.restore_provider_candidate(result)
            result = semantic_terminology.restore_provider_candidate(result)
        result = _apply_source_aware_corrections(prepared_text, result)

        final_obligation_evaluation = evaluate_canonical_obligations(
            result, canonical_obligations
        )
        common_failure = {
            "source_text": raw_text,
            "target_text": None,
            "result_source": "post_policy",
            "cache_status": cache_status,
            "incomplete": incomplete,
            "engine": engine.engine_name if engine else "",
            "model": engine.model_name if engine else "",
            "prompt_version": prompt_version,
        }
        if (
            _looks_like_meta_garbage_output(result)
            or _looks_like_model_refusal(result)
        ):
            log.debug(
                "Filtering meta garbage translation output: %.40s -> %.40s",
                prepared_text,
                result,
            )
            self._reset_failed_input()
            return TranslationOutcome(
                **common_failure,
                status="filtered",
                filter_reason="meta_garbage_output",
                canonical_obligation_evaluation=final_obligation_evaluation,
            )

        final_escrow_evaluation = unknown_name_escrow.evaluate_final(result)
        if not final_escrow_evaluation.passed:
            metrics.increment("translation.unknown_name_escrow.final_rejected")
            self._reset_failed_input()
            return TranslationOutcome(
                **common_failure,
                status="failed",
                filter_reason=final_escrow_evaluation.reason,
                canonical_obligation_evaluation=final_obligation_evaluation,
            )

        terminology_passed, terminology_reason = semantic_terminology.evaluate_final(
            result
        )
        if not terminology_passed:
            metrics.increment("translation.semantic_terminology.final_rejected")
            self._reset_failed_input()
            return TranslationOutcome(
                **common_failure,
                status="failed",
                filter_reason=terminology_reason,
            )

        if not final_obligation_evaluation.passed:
            metrics.increment("translation.canonical_obligation.final_rejected")
            self._reset_failed_input()
            return TranslationOutcome(
                **common_failure,
                status="failed",
                filter_reason="canonical_obligation_missing",
                canonical_obligation_evaluation=final_obligation_evaluation,
            )

        success_commit = lambda: self._record_success(
            prepared_text,
            result,
            incomplete,
            prompt_version,
            engine,
            history_cohort,
        )
        if not getattr(self, "_defer_success_record", False):
            success_commit()
            success_commit = None
        return TranslationOutcome(
            source_text=raw_text,
            target_text=result,
            status="success",
            result_source="provisional_promotion" if promoted else "api",
            cache_status=cache_status,
            incomplete=incomplete,
            engine=engine.engine_name if engine else "",
            model=engine.model_name if engine else "",
            prompt_version=prompt_version,
            canonical_obligation_evaluation=final_obligation_evaluation,
            unknown_name_approved_terms=unknown_name_escrow.approved_hangul_terms,
            deferred_success=success_commit,
        )

    def _policy_state(self) -> TranslationPolicy:
        return self._policy

    def _memory_state(self) -> TranslationMemory:
        return self._memory

    def _history_cohort(self) -> HistoryCohort:
        snapshot = bound_activity_snapshot()
        profile_snapshot = bound_profile_snapshot()
        return _history_cohort_for(
            snapshot,
            (
                profile_snapshot.cache_identity
                if profile_snapshot is not None
                else effective_profile_id(getattr(cfg, "active_streamer_profile", ""))
            ),
        )

    def _history_session(self) -> str:
        value = getattr(self, "_history_session_id", "")
        if not value:
            value = uuid.uuid4().hex[:12]
            self._history_session_id = value
        return value

    def _translate_slang(self, text: str, incomplete: bool) -> str | None:
        slang_result = self._policy_state().slang_result(text)
        if not slang_result:
            return None

        log.debug("Slang hit: %s → %s", text, slang_result)
        return slang_result

    def _record_direct_success(self, text: str, result: str, incomplete: bool,
                               cohort: HistoryCohort) -> None:
        with self._state_guard():
            self._memory_state().record_direct_memory(
                text, result, incomplete, cohort
            )
        # File I/O outside the shared lock (M1).
        self._memory_state().write_history(text, result)

    def _record_lookup_context(self, text: str, result: str, incomplete: bool,
                               cohort: HistoryCohort) -> None:
        with self._state_guard():
            self._memory_state().record_recent_context(
                text, result, incomplete, cohort
            )

    def _lookup_existing_translation_event(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        engine: TranslationEngine | None = None,
        cohort: HistoryCohort | None = None,
    ) -> MemoryLookup:
        # Memory cache under the lock; the SQLite read happens outside it (M1)
        # so a slow DB never blocks the other worker.
        if engine is None:
            engine = self._active_engine()
        with self._state_guard():
            lookup = self._memory_state().lookup_memory_event(
                text,
                incomplete,
                prompt_ver,
                engine,
                remember_recent=not getattr(self, "_defer_success_record", False),
                cohort=cohort,
            )
        if lookup.result:
            log.debug("Cache hit: %s", text[:20])
            return lookup
        if incomplete or engine is None or not _db_cache_enabled():
            metrics.increment("translation.cache.skipped")
            return MemoryLookup(None, "skipped")
        db_result = self._memory_state().db_lookup(text, engine, prompt_ver)
        if db_result:
            with self._state_guard():
                lookup = self._memory_state().record_db_hit(
                    text,
                    incomplete,
                    prompt_ver,
                    db_result,
                    engine,
                    remember_recent=not getattr(self, "_defer_success_record", False),
                    cohort=cohort,
                )
            log.debug("Cache hit: %s", text[:20])
            return lookup
        metrics.increment("translation.cache.miss")
        return MemoryLookup(None, "miss")

    def _record_success(self, text: str, result: str, incomplete: bool,
                        prompt_ver: str,
        engine: TranslationEngine | None = None,
        cohort: HistoryCohort | None = None) -> None:
        with self._state_guard():
            if engine is None:
                engine = self._active_engine()
            self._memory_state().record_success_memory(
                text, result, incomplete, prompt_ver, engine, cohort
            )
        # File/DB I/O outside the shared lock (M1).
        self._memory_state().write_history(text, result)
        if not incomplete and engine is not None and _db_cache_enabled():
            self._memory_state().db_store(text, result, engine, prompt_ver)

    def _invalidate_cached_translation(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
        result: str | None,
    ) -> None:
        with self._state_guard():
            self._memory_state().invalidate_memory(
                text, incomplete, prompt_ver, active_engine, result
            )
        # DB delete outside the shared lock (M1). The delete is NOT gated on
        # _db_cache_enabled(): stale rows must be purged even if the cache layer
        # was enabled earlier in the session and disabled since.
        if not incomplete and active_engine is not None:
            self._memory_state().invalidate_db(text, active_engine, prompt_ver)

    def _active_engine(self) -> TranslationEngine | None:
        return active_engine(self._engines, self._active_idx)

    def _log_prompt_mode_once(self) -> None:
        if _is_qwen_model() and not hasattr(self, '_qwen_log_once'):
            log.info("Using Qwen-optimized system prompt (shorter, more direct)")
            self._qwen_log_once = True

    def _build_system_prompt(self) -> str:
        return _compose_system_prompt()

    @staticmethod
    def _prompt_version(system_prompt: str) -> str:
        return hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    def _prompt_version_for_engine(
        self,
        engine: TranslationEngine | None,
        system_prompt: str,
    ) -> str:
        snapshot = bound_activity_snapshot()
        if snapshot is None:
            snapshot = capture_effective_activity_snapshot(
                getattr(cfg.translation, "current_activity", ""),
                automatic_enabled=bool(
                    getattr(cfg.scene, "publish_translation_activity", False)
                ),
            )
            with bind_activity_snapshot(snapshot):
                effective_prompt = effective_system_prompt_for_engine(
                    engine,
                    system_prompt,
                )
        else:
            effective_prompt = effective_system_prompt_for_engine(
                engine,
                system_prompt,
            )
        cohort = self._history_cohort()
        # Cache entries can be context-shaped, so the episode boundary is part
        # of the identity even when activity is unknown. Resolver refreshes do
        # not change cohort_epoch; actual scene/profile transitions do.
        return self._prompt_version(
            effective_prompt
            + "\n[canonical-publication-policy] "
            + _CANONICAL_PUBLICATION_POLICY_VERSION
            + "\n[request-cache-cohort] "
            + f"{cohort[0]}:{cohort[1]}:{cohort[2]}"
            + (
                "\n[history-session] " + self._history_session()
                if int(getattr(cfg.translation, "context_window", 0) or 0) > 0
                else ""
            )
            + (
                "\n[activity-cache-identity] " + snapshot.cache_identity
                if snapshot.activity_id
                else ""
            )
        )

    def _call_with_fallback(
        self, text: str, system_prompt: str, incomplete: bool,
        history: list[tuple[str, str]] | None = None,
        *,
        deadline_at: float | None = None,
        frozen_messages_by_engine: dict[
            str, tuple[tuple[str, str], ...]
        ] | None = None,
        canonical_obligations: tuple[CanonicalObligation, ...] | None = None,
        source_text: str | None = None,
        unknown_name_escrow: UnknownNameEscrow | None = None,
        semantic_terminology: SemanticTerminologyEscrow | None = None,
    ) -> tuple[str | None, TranslationEngine | None]:
        """Returns (result, engine_used). engine_used is the engine that
        actually produced the result — on a soft fallback this differs from
        the active engine, which intentionally stays on primary."""
        source_text = source_text or text
        if canonical_obligations is None:
            canonical_obligations = _resolve_active_canonical_obligations(source_text)
        if unknown_name_escrow is None:
            unknown_name_escrow = UnknownNameEscrow(source_text, source_text)
        if semantic_terminology is None:
            semantic_terminology = SemanticTerminologyEscrow(
                source_text, source_text
            )
        fallback_state = self._fallback_state()
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            before_state = _copy_fallback_state(fallback_state)
            state = fallback_state
        else:
            with lock:
                before_state = _copy_fallback_state(fallback_state)
                state = _copy_fallback_state(before_state)
        result, used_idx = call_with_fallback(
            self._engines,
            state,
            text,
            system_prompt,
            incomplete,
            history,
            _fallback_failure_threshold(),
            _looks_untranslated,
            log,
            circuit_breaker_enabled=_translation_circuit_breaker_enabled(),
            recovery_cooldown_seconds=(
                cfg.translation.circuit_recovery_cooldown_sec
            ),
            deadline_at=deadline_at,
            max_route_inflight=cfg.translation.live_route_max_inflight,
            frozen_messages_by_engine=frozen_messages_by_engine,
            output_guard=lambda candidate_engine, candidate, _provider_source: (
                _translation_output_guard(
                    candidate_engine,
                    candidate,
                    source_text,
                    obligations=canonical_obligations,
                    unknown_name_escrow=unknown_name_escrow,
                    semantic_terminology=semantic_terminology,
                )
            ),
        )
        if lock is not None:
            with lock:
                committed_before = _copy_fallback_state(fallback_state)
                _merge_fallback_state(fallback_state, before_state, state)
                committed_after = _copy_fallback_state(fallback_state)
        else:
            committed_before = before_state
            committed_after = _copy_fallback_state(fallback_state)

        if (
            _translation_circuit_breaker_enabled()
            and committed_after.active_idx != committed_before.active_idx
        ):
            from_engine = active_engine(self._engines, committed_before.active_idx)
            to_engine = active_engine(self._engines, committed_after.active_idx)
            attempts = get_translation_attempts()
            failed_attempt = next(
                (
                    attempt
                    for attempt in reversed(attempts)
                    if str(attempt.get("route_id") or "")
                    == translation_route_id(from_engine)
                ),
                {},
            )
            _send_fallback_event(
                getattr(self, "_shared_state", None),
                (
                    "fallback_recovered"
                    if committed_after.active_idx < committed_before.active_idx
                    else "circuit_opened"
                    if committed_before.active_idx == 0
                    else "fallback_advanced"
                ),
                primary_engine=str(getattr(active_engine(self._engines, 0), "engine_name", "") or ""),
                primary_route=translation_route_id(active_engine(self._engines, 0)),
                from_engine=str(getattr(from_engine, "engine_name", "") or ""),
                from_route=translation_route_id(from_engine),
                active_engine=str(getattr(to_engine, "engine_name", "") or ""),
                active_route=translation_route_id(to_engine),
                from_active_idx=committed_before.active_idx,
                active_idx=committed_after.active_idx,
                sequence_id=getattr(self, "_current_sequence_id", None),
                attempt_chain=[
                    {
                        key: attempt.get(key)
                        for key in (
                            "route_id",
                            "status",
                            "failure_scope",
                            "api_error_type",
                            "api_error_message_class",
                        )
                    }
                    for attempt in attempts
                ],
                failure_status=str(failed_attempt.get("status") or ""),
                failure_scope=str(failed_attempt.get("failure_scope") or ""),
                api_error_type=failed_attempt.get("api_error_type"),
                api_error_message_class=failed_attempt.get("api_error_message_class"),
                cooldown_seconds=cfg.translation.circuit_recovery_cooldown_sec,
                cooldown_remaining_ms=round(
                    max(0.0, committed_after.primary_cooldown_until - time.monotonic()) * 1000,
                    2,
                ),
                required_probe_successes=(
                    cfg.translation.circuit_recovery_success_threshold
                ),
            )
        used_engine = active_engine(self._engines, used_idx)
        return result, used_engine


    def _get_prompt_version_hash(self) -> str:
        snapshot = bound_activity_snapshot() or capture_effective_activity_snapshot(
            getattr(cfg.translation, "current_activity", ""),
            automatic_enabled=bool(
                getattr(cfg.scene, "publish_translation_activity", False)
            ),
        )
        with bind_activity_snapshot(snapshot):
            return self._prompt_version_for_engine(
                self._active_engine(),
                self._build_system_prompt(),
            )


_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window
_MAX_COMPLETED_BACKLOG = 64  # completed-but-unemitted results before warning (L8)
_STOP_DRAIN_TIMEOUT_MARGIN_SEC = 0.5


def _translation_max_output_delay_ms() -> float:
    value = getattr(cfg.translation, "max_subtitle_output_delay_ms", 30000)
    return float(value) if isinstance(value, (int, float)) else 30000.0


def _stop_drain_timeout_sec() -> float:
    try:
        join_timeout = float(getattr(cfg, "thread_join_timeout", 5.0) or 5.0)
    except (TypeError, ValueError):
        join_timeout = 5.0
    return max(_TRANSLATION_LOOP_POLL_SEC, join_timeout - _STOP_DRAIN_TIMEOUT_MARGIN_SEC)


def _compose_system_prompt() -> str:
    """Shared between Translator._build_system_prompt and the probe thread (L5).

    Pure function of config since PromptEvolver was removed (2026-06-12):
    prompt_ver is now stable for a given base prompt + profile selection.
    """
    is_qwen = _is_qwen_model()
    system_prompt = _QWEN_PROMPT if is_qwen else _BASE_PROMPT

    if effective_profile_applied(cfg.translation.use_profile):
        profile_id = effective_profile_id(cfg.active_streamer_profile)
        streamer_profile = get_translation_profile(profile_id, qwen=is_qwen)
        if streamer_profile:
            system_prompt += "\n\n" + streamer_profile
            log.debug("Appended streamer profile: %s", profile_id)

    # Manual session state (orthogonal to profiles, applies even with
    # use_profile=False): one labeled background line, never source text.
    activity_capsule = activity_prompt_capsule(
        effective_activity_value(getattr(cfg.translation, "current_activity", ""))
    )
    if activity_capsule:
        system_prompt += "\n\n" + activity_capsule

    # Output rules go last so profile/background sections never sit after the
    # final instruction the model is supposed to obey.
    system_prompt += _QWEN_PROMPT_TAIL if is_qwen else _BASE_PROMPT_TAIL
    return system_prompt


def _build_probe_system_prompt(_shared_state: _TranslatorSharedState) -> str:
    return _compose_system_prompt()


def _start_fallback_probe_thread(
    shared_state: _TranslatorSharedState,
    stop_event: threading.Event,
    *,
    interval_seconds: float = _FALLBACK_PROBE_INTERVAL_SEC,
) -> threading.Thread:
    def run() -> None:
        engines: list[TranslationEngine] | None = None
        engines_key: tuple | None = None
        while not stop_event.wait(interval_seconds):
            with shared_state.lock:
                if shared_state.fallback.active_idx <= 0:
                    continue
                probe_state = _copy_fallback_state(shared_state.fallback)
                before_state = _copy_fallback_state(probe_state)
                # Provider-health probes are not subtitles and have no event
                # cohort. Conversation history would only risk cross-scene
                # contamination, so probes deliberately run without it.
                probe_history: list[tuple[str, str]] = []
            # L7: rebuild the probe chain when the engine selection changes
            # mid-run instead of caching the first build forever.
            chain_key = engine_chain_config_key()
            if engines is None or chain_key != engines_key:
                engines = _build_engine_chain()
                engines_key = chain_key
            probe_observations: list[dict[str, object]] = []
            probe_started = time.monotonic()
            activity_snapshot = capture_effective_activity_snapshot(
                getattr(cfg.translation, "current_activity", ""),
                automatic_enabled=bool(
                    getattr(cfg.scene, "publish_translation_activity", False)
                ),
            )
            with bind_activity_snapshot(activity_snapshot):
                system_prompt = _build_probe_system_prompt(shared_state)
                try:
                    probe_primary_recovery(
                        engines,
                        probe_state,
                        _FALLBACK_PROBE_TEXT,
                        system_prompt,
                        _looks_untranslated,
                        log,
                        circuit_breaker_enabled=(
                            _translation_circuit_breaker_enabled()
                        ),
                        recovery_cooldown_seconds=(
                            cfg.translation.circuit_recovery_cooldown_sec
                        ),
                        required_consecutive_successes=(
                            cfg.translation.circuit_recovery_success_threshold
                        ),
                        history=probe_history,
                        observation_sink=probe_observations.append,
                        deadline_at=_translation_deadline_at(),
                        max_route_inflight=(
                            cfg.translation.live_route_max_inflight
                        ),
                        output_guard=_translation_output_guard,
                    )
                except Exception:
                    log.exception("Fallback primary probe failed unexpectedly")
                    continue
            probe_elapsed_ms = round((time.monotonic() - probe_started) * 1000, 2)
            with shared_state.lock:
                committed_before = _copy_fallback_state(shared_state.fallback)
                _merge_fallback_state(shared_state.fallback, before_state, probe_state)
                committed_after = _copy_fallback_state(shared_state.fallback)

            observation = probe_observations[-1] if probe_observations else {}
            probe_status = str(observation.get("status") or "unknown")
            state_applied = committed_after == probe_state
            active_before = active_engine(engines, committed_before.active_idx)
            active_after = active_engine(engines, committed_after.active_idx)
            common_fields = {
                "primary_engine": str(getattr(active_engine(engines, 0), "engine_name", "") or ""),
                "primary_route": translation_route_id(active_engine(engines, 0)),
                "active_engine": str(getattr(active_after, "engine_name", "") or ""),
                "active_route": translation_route_id(active_after),
                "active_engine_before": str(getattr(active_before, "engine_name", "") or ""),
                "active_route_before": translation_route_id(active_before),
                "active_idx_before": committed_before.active_idx,
                "active_idx": committed_after.active_idx,
                "probe_status": probe_status,
                "probe_elapsed_ms": probe_elapsed_ms,
                "probe_history_items": len(probe_history),
                "probe_history_source_chars": sum(len(source) for source, _ in probe_history),
                "probe_history_target_chars": sum(len(target) for _, target in probe_history),
                "probe_success_streak": observation.get("success_streak", 0),
                "committed_probe_success_streak": committed_after.consecutive_probe_successes,
                "required_probe_successes": (
                    cfg.translation.circuit_recovery_success_threshold
                ),
                "cooldown_seconds": (
                    cfg.translation.circuit_recovery_cooldown_sec
                ),
                "cooldown_remaining_ms": round(
                    max(0.0, committed_after.primary_cooldown_until - time.monotonic()) * 1000,
                    2,
                ),
                "state_applied": state_applied,
            }
            if observation.get("exception_type"):
                common_fields["exception_type"] = observation["exception_type"]
            for diagnostic_field in (
                "api_error_type",
                "api_error_message_class",
                "api_total_wall_ms",
                "api_timeout_count",
                "deadline_exceeded",
                "deadline_scope",
                "deadline_budget_ms",
            ):
                if diagnostic_field in observation:
                    common_fields[diagnostic_field] = observation[
                        diagnostic_field
                    ]
            if probe_status == "cooldown_skipped":
                action = "probe_cooldown_skipped"
            elif probe_status == "success":
                action = "probe_succeeded"
            else:
                action = "probe_failed"
            _send_fallback_event(shared_state, action, **common_fields)
            if (
                bool(observation.get("recovered"))
                and committed_before.active_idx > 0
                and committed_after.active_idx == 0
            ):
                _send_fallback_event(shared_state, "circuit_closed", **common_fields)

    return start_daemon_thread("TranslationFallbackProbe", run)


def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None,
          *,
          provisional_queue: queue.Queue | None = None) -> threading.Thread:
    def run():
        shared_state = _new_translator_shared_state(
            fallback_event_sink=_emit_fallback_runtime_event,
        )
        worker_state = threading.local()
        executor = _DaemonWorkerPool(
            max_workers=_TRANSLATION_WORKERS,
            thread_name_prefix="TranslationWorker",
        )
        provisional_executor = _DaemonWorkerPool(
            max_workers=1,
            thread_name_prefix="ProvisionalTranslation",
        )
        provisional_store = ProvisionalStore()
        provisional_future: Future[None] | None = None
        pending: dict[int, Future[_CompletedTranslation]] = {}
        completed: dict[int, _CompletedTranslation] = {}
        next_seq = 0
        next_emit_seq = 0
        last_result = ""
        last_result_time = 0.0
        _start_fallback_probe_thread(shared_state, stop_event)

        def translate_provisional(request: ProvisionalRequest) -> None:
            started = time.monotonic()
            if str(getattr(cfg.translation, "deepseek_route", "off")) != "primary":
                return
            if (
                request.profile_snapshot is not None
                and request.profile_snapshot.cache_identity
                != profile_state.current().cache_identity
            ):
                provisional_store.close(request.provisional_id)
                runtime_events.emit(
                    "provisional_translation",
                    action="retired_profile_generation",
                    provisional_id=request.provisional_id,
                    **request.profile_snapshot.as_metadata(),
                )
                return
            engine = DeepSeekTranslationEngine()
            if not engine.available or provisional_store.is_closed(
                request.provisional_id
            ):
                return
            reset_last_engine_diagnostics()
            reset_last_token_usage()
            try:
                request_profile_snapshot = request.profile_snapshot or profile_state.current()
                with bind_profile_snapshot(request_profile_snapshot):
                  with bind_profile_id(request.profile_id):
                    with bind_activity_snapshot(request.activity_snapshot):
                        preview_translator = Translator(shared_state=shared_state)
                        preview_policy = _new_translation_policy()
                        repetition_evidence = RepetitionEvidence(
                            min_avg_logprob=request.min_avg_logprob,
                            max_no_speech_prob=request.max_no_speech_prob,
                            cut_reason=request.cut_reason,
                            forced=request.forced,
                            incomplete=request.incomplete,
                        )
                        filter_reason = preview_policy.rejection_reason(
                            request.text.strip(),
                            repetition_evidence=repetition_evidence,
                        )
                        prepared_source = preview_policy.prepare_input(
                            request.text.strip(),
                            initial_rejection_reason=filter_reason,
                            repetition_evidence=repetition_evidence,
                        )
                        if prepared_source is None:
                            runtime_events.emit(
                                "provisional_translation",
                                action="source_filtered",
                                provisional_id=request.provisional_id,
                                filter_reason=filter_reason or "source_policy",
                            )
                            return
                        prepared_source = _normalize_source_before_matching(
                            prepared_source
                        )
                        obligations = _resolve_active_canonical_obligations(
                            prepared_source
                        )
                        known_source_spans = tuple(
                            span
                            for obligation in obligations
                            for span in obligation.source_spans
                        )
                        unknown_name_escrow = resolve_unknown_name_escrow(
                            prepared_source,
                            known_source_spans=known_source_spans,
                        )
                        semantic_terminology = resolve_semantic_terminology(
                            unknown_name_escrow.provider_source
                        )
                        system_prompt = preview_translator._build_system_prompt()
                        history_cohort = _history_cohort_for(
                            request.activity_snapshot,
                            request_profile_snapshot.cache_identity,
                        )
                        with shared_state.lock:
                            history = shared_state.memory.context(history_cohort)
                        messages = build_effective_deepseek_messages(
                            semantic_terminology.provider_source,
                            system_prompt,
                            request.incomplete,
                            history,
                        )
                        fingerprint = provisional_fingerprint(
                            prepared_source=prepared_source,
                            source_utterance_ids=request.source_utterance_ids,
                            evidence_source_utterance_ids=(
                                request.evidence_source_utterance_ids
                            ),
                            profile_id=effective_profile_id(request.profile_id),
                            profile_cache_identity=request_profile_snapshot.cache_identity,
                            activity_cache_identity=(
                                request.activity_snapshot.cache_identity
                            ),
                            history_cohort=history_cohort,
                            messages=messages,
                            incomplete=request.incomplete,
                        )
                        # Re-check at the actual call boundary so stale queued work
                        # cannot reach DeepSeek after the emergency route is disabled.
                        if str(getattr(cfg.translation, "deepseek_route", "off")) != "primary":
                            return
                        if (
                            request.profile_snapshot is not None
                            and request.profile_snapshot.cache_identity
                            != profile_state.current().cache_identity
                        ):
                            provisional_store.close(request.provisional_id)
                            runtime_events.emit(
                                "provisional_translation",
                                action="retired_profile_generation",
                                provisional_id=request.provisional_id,
                                **request.profile_snapshot.as_metadata(),
                            )
                            return
                        raw_target = engine.translate_messages(messages)
                        if (
                            request.profile_snapshot is not None
                            and request.profile_snapshot.cache_identity
                            != profile_state.current().cache_identity
                        ):
                            provisional_store.close(request.provisional_id)
                            runtime_events.emit(
                                "provisional_translation",
                                action="retired_profile_generation",
                                provisional_id=request.provisional_id,
                                **request.profile_snapshot.as_metadata(),
                            )
                            return
                        diagnostics = get_last_engine_api_diagnostics()
                        usage = get_last_token_usage()
                        if not raw_target:
                            runtime_events.emit(
                                "provisional_translation",
                                action="failed",
                                provisional_id=request.provisional_id,
                                latency_ms=round((time.monotonic() - started) * 1000, 2),
                                engine=engine.engine_name,
                                model=engine.model_name,
                            )
                            return
                        guard = _translation_output_guard(
                            engine,
                            raw_target,
                            prepared_source,
                            obligations=obligations,
                            unknown_name_escrow=unknown_name_escrow,
                            semantic_terminology=semantic_terminology,
                        )
                        if guard.get("reason"):
                            runtime_events.emit(
                                "provisional_translation",
                                action="guard_rejected",
                                provisional_id=request.provisional_id,
                                guard_rejection=str(guard.get("reason") or ""),
                                latency_ms=round((time.monotonic() - started) * 1000, 2),
                                engine=engine.engine_name,
                                model=engine.model_name,
                            )
                            return
                        display_target = str(
                            guard.get("candidate_output") or raw_target
                        )
                        completed = time.monotonic()
                        candidate = ProvisionalCandidate(
                            provisional_id=request.provisional_id,
                            raw_target=raw_target,
                            display_target=display_target,
                            fingerprint=fingerprint,
                            engine=engine.engine_name,
                            model=engine.model_name,
                            requested_at_monotonic=request.requested_at_monotonic,
                            completed_at_monotonic=completed,
                            usage=usage,
                            diagnostics=diagnostics,
                        )
                        preview_payload = SubtitlePayload(
                            text=display_target,
                            subtitle_id=request.provisional_id,
                            revision=0,
                            phase="provisional",
                        )
                        if not provisional_store.publish_and_enqueue(
                            candidate,
                            lambda: put_latest(
                                subtitle_queue,
                                preview_payload,
                                log,
                                "subtitle_queue",
                            ),
                        ):
                            runtime_events.emit(
                                "provisional_translation",
                                action="cancelled_late",
                                provisional_id=request.provisional_id,
                            )
                            return
                        runtime_events.emit(
                            "provisional_translation",
                            action="succeeded",
                            provisional_id=request.provisional_id,
                            target_text=display_target,
                            latency_ms=round((completed - started) * 1000, 2),
                            stt_ready_to_subtitle_ms=round(
                                max(
                                    0.0,
                                    completed
                                    - request.first_stt_ready_at_monotonic,
                                )
                                * 1000,
                                2,
                            ),
                            engine=engine.engine_name,
                            model=engine.model_name,
                            input_tokens=usage.get("prompt"),
                            output_tokens=usage.get("output"),
                            cache_hit_tokens=usage.get("cache_read"),
                            cache_miss_tokens=usage.get("cache_write"),
                            cost_usd=diagnostics.get("api_cost_usd"),
                        )
            except Exception:
                log.exception("Provisional translation failed")
                runtime_events.emit(
                    "provisional_translation",
                    action="failed",
                    provisional_id=request.provisional_id,
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                )

        def translate_item(
            seq: int, item, submitted_at: float, submitted_at_utc: str
        ) -> _CompletedTranslation:
            started = time.monotonic()
            worker_started_at_utc = datetime.now(timezone.utc).isoformat()
            worker_id = threading.current_thread().name
            # Reset before translating so cache hits / failures (which never reach an
            # engine) don't inherit the previous call's token usage / corrections.
            reset_last_engine_diagnostics()
            reset_last_token_usage()
            reset_translation_call_trace()
            reset_corrections()
            # Everything that touches `item` runs inside the try: a malformed item
            # must yield a synthetic failed outcome, never a lost sequence number
            # (a gap would stall the in-order emit loop forever).
            text = ""
            incomplete = False
            policy_input = ""
            worker_translator: Translator | None = None
            translation_mode = str(
                getattr(cfg.translation, "translation_mode", "") or ""
            )
            metadata: dict = {"translation_mode": translation_mode}
            try:
                text = sentence_text(item)
                incomplete = sentence_incomplete(item)
                metadata = sentence_metadata(item).copy()
                event_snapshot = metadata.pop("activity_snapshot", None)
                event_profile_snapshot = metadata.pop("profile_snapshot", None)
                activity_snapshot_fallback_used = not isinstance(
                    event_snapshot, ActivitySnapshot
                )
                activity_snapshot = (
                    event_snapshot
                    if isinstance(event_snapshot, ActivitySnapshot)
                    else capture_effective_activity_snapshot(
                        getattr(cfg.translation, "current_activity", ""),
                        automatic_enabled=bool(
                            getattr(cfg.scene, "publish_translation_activity", False)
                        ),
                        source_text=text,
                    )
                )
                worker_observed_snapshot = capture_effective_activity_snapshot(
                    getattr(cfg.translation, "current_activity", ""),
                    automatic_enabled=bool(
                        getattr(cfg.scene, "publish_translation_activity", False)
                    ),
                )
                profile_snapshot = (
                    event_profile_snapshot
                    if isinstance(event_profile_snapshot, ProfileSnapshot)
                    else profile_state.legacy_snapshot(
                        str(metadata.get("profile_id") or getattr(cfg, "active_streamer_profile", "") or ""),
                        translation_profile_applied=bool(cfg.translation.use_profile),
                        stt_glossary_applied=bool(cfg.stt.use_profile_glossary),
                    )
                )
                profile_id = profile_snapshot.effective_profile_id
                marker = _dependency_marker(text)
                metadata.update(
                    {
                        "sequence_id": seq,
                        "starts_with_dependency_marker": bool(marker),
                        "dependency_marker": marker,
                        "profile_id": profile_id,
                        **profile_snapshot.as_metadata(),
                        # QE must be able to check whether background context
                        # helped or polluted; record it per event.
                        "current_activity": activity_snapshot.display_label,
                        **activity_snapshot_metadata(activity_snapshot),
                        "activity_snapshot_stage": (
                            "sentence_enqueue"
                            if not activity_snapshot_fallback_used
                            else "worker_fallback"
                        ),
                        "activity_bound_snapshot_used": (
                            not activity_snapshot_fallback_used
                        ),
                        "activity_snapshot_fallback_used": (
                            activity_snapshot_fallback_used
                        ),
                        "activity_snapshot_fallback_reason": (
                            "legacy_event_without_snapshot"
                            if activity_snapshot_fallback_used
                            else ""
                        ),
                        "activity_local_cue_applied": (
                            activity_snapshot.source == "local_source"
                        ),
                        "worker_observed_activity_id": (
                            worker_observed_snapshot.activity_id
                        ),
                        "worker_observed_activity_source": (
                            worker_observed_snapshot.source
                        ),
                        "worker_observed_effective_generation": (
                            worker_observed_snapshot.effective_generation
                        ),
                        "activity_capsule_applied": bool(
                            activity_snapshot.display_label
                        ),
                        "activity_capsule_activity_id": (
                            activity_snapshot.activity_id
                            if activity_snapshot.display_label
                            else ""
                        ),
                        "worker_started_at_utc": worker_started_at_utc,
                        "translation_submitted_at_utc": submitted_at_utc,
                        # Diagnostic-only: distinguishes the 5s live timeout path
                        # from the 60s clip/offline path in latency artifacts.
                        "translation_mode": translation_mode,
                    }
                )
                worker_translator = getattr(worker_state, "translator", None)
                if worker_translator is None:
                    worker_translator = Translator(shared_state=shared_state)
                    worker_translator._defer_success_record = True
                    worker_state.translator = worker_translator
                repetition_evidence = RepetitionEvidence(
                    min_avg_logprob=metadata.get("min_avg_logprob"),
                    max_no_speech_prob=metadata.get("max_no_speech_prob"),
                    cut_reason=str(metadata.get("cut_reason") or ""),
                    forced=bool(metadata.get("forced", False)),
                    incomplete=incomplete,
                )
                source_key = (text or "").strip()
                provisional_id = str(metadata.get("provisional_id") or "")
                provisional_candidate = provisional_store.candidate(provisional_id)
                provisional_not_ready = bool(
                    provisional_id and provisional_candidate is None
                )
                if provisional_id and provisional_candidate is None:
                    provisional_store.close(provisional_id)
                with shared_state.lock:
                    inflight_count = shared_state.inflight_sources.get(source_key, 0)
                    if inflight_count and shared_state.policy.last_input == source_key:
                        # The previous identical input has not succeeded yet.
                        # Let this worker attempt it; ordered output dedupe still
                        # suppresses two successful visible subtitles.
                        shared_state.policy.reset_last_input()
                    shared_state.inflight_sources[source_key] = inflight_count + 1
                outcome_for_state: TranslationOutcome | None = None
                try:
                    worker_translator._current_sequence_id = seq
                    with bind_profile_snapshot(profile_snapshot):
                      with bind_profile_id(profile_id):
                        with bind_activity_snapshot(activity_snapshot):
                            history_cohort = _history_cohort_for(
                                activity_snapshot, profile_snapshot.cache_identity
                            )
                            with shared_state.lock:
                                history_items, cross_cohort_items = (
                                    shared_state.memory.cohort_stats(history_cohort)
                                )
                            metadata.update(
                                {
                                    "history_profile_id": history_cohort[0],
                                    "history_activity_id": history_cohort[1],
                                    "history_cohort_epoch": history_cohort[2],
                                    "history_cohort_id": (
                                        f"{history_cohort[0]}:"
                                        f"{history_cohort[1]}:"
                                        f"{history_cohort[2]}"
                                    ),
                                    "history_candidate_count": history_items,
                                    "history_cross_cohort_excluded_count": (
                                        cross_cohort_items
                                    ),
                                }
                            )
                            if provisional_candidate is None:
                                # Preserve the long-standing worker contract for
                                # ordinary sentences and lightweight test doubles.
                                outcome = worker_translator.translate_event(
                                    text,
                                    incomplete,
                                    repetition_evidence=repetition_evidence,
                                )
                            else:
                                outcome = worker_translator.translate_event(
                                    text,
                                    incomplete,
                                    repetition_evidence=repetition_evidence,
                                    provisional_candidate=provisional_candidate,
                                    source_utterance_ids=tuple(
                                        metadata.get("source_utterance_ids") or ()
                                    ),
                                    evidence_source_utterance_ids=tuple(
                                        metadata.get(
                                            "evidence_source_utterance_ids"
                                        )
                                        or ()
                                    ),
                                )
                            outcome_for_state = outcome
                            if provisional_id:
                                provisional_store.close(provisional_id)
                                provisional_trace = {
                                    "provisional_id": provisional_id,
                                    **worker_translator._last_provisional_trace,
                                }
                                if provisional_not_ready and not worker_translator._last_provisional_trace:
                                    provisional_trace.update(
                                        {
                                            "promotion_attempted": False,
                                            "promotion_passed": False,
                                            "candidate_not_ready": True,
                                            "final_retranslation": True,
                                        }
                                    )
                                metadata["provisional"] = provisional_trace
                                metadata.update(
                                    {
                                        "provisional_id": provisional_id,
                                        "provisional_promotion_attempted": bool(
                                            provisional_trace.get(
                                                "promotion_attempted"
                                            )
                                        ),
                                        "provisional_promotion_passed": bool(
                                            provisional_trace.get("promotion_passed")
                                        ),
                                        "provisional_fingerprint_mismatch": bool(
                                            provisional_trace.get(
                                                "fingerprint_mismatch"
                                            )
                                        ),
                                        "provisional_guard_rejection": str(
                                            provisional_trace.get(
                                                "guard_rejection"
                                            )
                                            or ""
                                        ),
                                        "provisional_final_retranslation": bool(
                                            provisional_trace.get(
                                                "final_retranslation"
                                            )
                                        ),
                                        "provisional_final_revision": 1,
                                    }
                                )
                            policy_input = (
                                getattr(worker_translator, "_last_input", "")
                                or source_key
                            )
                finally:
                    worker_translator._current_sequence_id = None
                    with shared_state.lock:
                        remaining = shared_state.inflight_sources.get(source_key, 1) - 1
                        if remaining > 0:
                            shared_state.inflight_sources[source_key] = remaining
                        else:
                            shared_state.inflight_sources.pop(source_key, None)
            except Exception:
                log.exception("Translation worker failed for: %.40s", text)
                failed_policy_input = policy_input or str(
                    getattr(worker_translator, "_last_input", "") or ""
                )
                if failed_policy_input:
                    with shared_state.lock:
                        if shared_state.policy.last_input == failed_policy_input:
                            shared_state.policy.reset_last_input()
                    policy_input = failed_policy_input
                metadata.setdefault("sequence_id", seq)
                outcome = TranslationOutcome(
                    source_text=(text or "").strip(),
                    target_text=None,
                    status="failed",
                    result_source="none",
                    cache_status="skipped",
                    incomplete=incomplete,
                )
            completed_at = time.monotonic()
            metadata["worker_completed_at_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            snapshot_monotonic = float(
                metadata.get("activity_snapshot_captured_at_monotonic") or 0.0
            )
            if snapshot_monotonic > 0:
                metadata["activity_snapshot_to_worker_ms"] = round(
                    max(0.0, started - snapshot_monotonic) * 1000, 2
                )
            elapsed = completed_at - started
            selected_attempt = get_selected_translation_attempt()
            diagnostics = selected_attempt or get_last_engine_diagnostics()
            api_diagnostics = selected_attempt or get_last_engine_api_diagnostics()
            attempts = get_translation_attempts()
            if attempts:
                metadata["attempts"] = attempts
            for usage_key, usage_value in _token_usage_for_outcome(outcome).items():
                if usage_value is not None:
                    metadata[f"token_{usage_key}"] = usage_value
            corrections = get_corrections()
            if corrections:
                metadata["corrections"] = corrections
                metadata["correction_count"] = len(corrections)
            retry_count = 0
            retry_reason = ""
            if _retry_diagnostics_apply(outcome, diagnostics):
                retry_count = int(diagnostics.get("retry_count") or 0)
                retry_reason = str(diagnostics.get("retry_reason") or "")
            api_event_fields = _api_event_fields(outcome, api_diagnostics)
            return _CompletedTranslation(
                seq,
                outcome,
                elapsed,
                metadata,
                submitted_at,
                started,
                completed_at,
                worker_id,
                retry_count,
                retry_reason,
                api_event_fields,
                policy_input,
            )

        def _failed_completion(seq: int) -> _CompletedTranslation:
            """Synthetic failed result so a crashed future never leaves a gap in
            the sequence — a gap would stall the in-order emit loop forever."""
            now = time.monotonic()
            return _CompletedTranslation(
                seq,
                TranslationOutcome(
                    source_text="",
                    target_text=None,
                    status="failed",
                    result_source="none",
                    cache_status="skipped",
                    incomplete=False,
                ),
                0.0,
                {
                    "sequence_id": seq,
                    "translation_mode": str(
                        getattr(cfg.translation, "translation_mode", "") or ""
                    ),
                },
                now,
                now,
                now,
                "",
                0,
                "",
                dict(_API_EVENT_DEFAULTS),
            )

        def collect_finished() -> None:
            for seq, future in list(pending.items()):
                if not future.done():
                    continue
                pending.pop(seq)
                try:
                    completed[seq] = future.result()
                except Exception:
                    log.exception("Translation future failed")
                    metrics.increment("translation.future_failed")
                    completed[seq] = _failed_completion(seq)
            if len(completed) > _MAX_COMPLETED_BACKLOG:
                # L8: _MAX_PENDING_TRANSLATIONS only bounds in-flight futures;
                # an emit-loop stall would let this dict grow unboundedly.
                metrics.increment("translation.completed_backlog_high")
                log.warning(
                    "Completed-translation backlog at %d (next_emit_seq=%d) — emit loop may be stalled",
                    len(completed),
                    next_emit_seq,
                )

        def emit_completed(item: _CompletedTranslation) -> None:
            nonlocal last_result, last_result_time
            outcome = item.outcome
            elapsed = item.elapsed
            emitted_at = time.monotonic()
            output_delay_ms = round(max(0.0, emitted_at - item.submitted_at) * 1000, 2)
            event_metadata = item.metadata.copy()
            sentence_enqueued_at = float(
                event_metadata.get("sentence_enqueued_at_monotonic") or 0.0
            )
            event_metadata.update(
                {
                    "engine_latency_ms": round(elapsed * 1000, 2),
                    "queue_wait_ms": round(max(0.0, item.started_at - item.submitted_at) * 1000, 2),
                    "sentence_queue_wait_ms": (
                        round(
                            max(0.0, item.started_at - sentence_enqueued_at) * 1000,
                            2,
                        )
                        if sentence_enqueued_at > 0
                        else None
                    ),
                    "output_delay_ms": output_delay_ms,
                    "predecessor_stall_ms": round(max(0.0, emitted_at - item.completed_at) * 1000, 2),
                    "translation_worker_id": item.worker_id,
                    "retry_count": item.retry_count,
                    "retry_reason": item.retry_reason,
                    **item.api_event_fields,
                }
            )
            metrics.observe_latency("translation", elapsed)
            event_fields = outcome.as_event_fields(elapsed * 1000, event_metadata)
            result = outcome.target_text
            final_script_rejection = (
                _final_script_rejection_reason(event_fields) if result else ""
            )
            if final_script_rejection:
                metrics.increment("translation.final_script_rejected")
                with shared_state.lock:
                    if (
                        item.policy_input
                        and shared_state.policy.last_input == item.policy_input
                    ):
                        shared_state.policy.reset_last_input()
                runtime_events.emit(
                    "translation",
                    **{
                        **event_fields,
                        "target_text": None,
                        "status": "failed",
                        "filter_reason": final_script_rejection,
                    },
                    subtitle_emitted=False,
                    subtitle_suppressed_reason="final_script_invariant",
                )
                return
            with shared_state.lock:
                if result:
                    shared_state.policy.last_input = (
                        item.policy_input or outcome.source_text.strip()
                    )
                elif (
                    outcome.status == "failed"
                    and item.policy_input
                    and shared_state.policy.last_input == item.policy_input
                ):
                    shared_state.policy.reset_last_input()
            if outcome.deferred_success is not None:
                try:
                    # Commit conversation state in sequence order, not provider
                    # completion order. Subtitle/event ordering already uses
                    # this same coordinator boundary.
                    outcome.deferred_success()
                except Exception:
                    metrics.increment("translation.memory_commit_failed")
                    log.exception(
                        "Ordered translation memory commit failed for sequence %s",
                        item.seq,
                    )
            if result:
                metrics.increment("translation.success")
                # Surface low-quality output in the 60 s metrics summary so a
                # degrading stretch is visible without scraping the JSONL.
                severity = event_fields.get("quality_severity")
                if severity in ("bad", "warn"):
                    metrics.increment(f"translation.quality.{severity}")
                now = time.monotonic()
                if result == last_result and (now - last_result_time) < _DEDUP_SUBTITLE_SEC:
                    log.debug("Suppressing duplicate subtitle: %s", result[:30])
                    runtime_events.emit(
                        "translation",
                        **event_fields,
                        subtitle_emitted=False,
                        subtitle_suppressed_reason="duplicate",
                    )
                    return
                max_output_delay_ms = _translation_max_output_delay_ms()
                if max_output_delay_ms > 0 and output_delay_ms > max_output_delay_ms:
                    metrics.increment("translation.subtitle.stale_skipped")
                    log.warning(
                        "Skipping stale subtitle after %.0fms output delay: %s",
                        output_delay_ms,
                        result[:30],
                    )
                    runtime_events.emit(
                        "translation",
                        **event_fields,
                        subtitle_emitted=False,
                        subtitle_suppressed_reason="stale_output_delay",
                    )
                    return
                last_result = result
                last_result_time = now
                provisional_id = str(event_metadata.get("provisional_id") or "")
                subtitle_item: str | SubtitlePayload = result
                if provisional_id:
                    subtitle_item = SubtitlePayload(
                        text=result,
                        subtitle_id=provisional_id,
                        revision=1,
                        phase="final",
                    )
                put_latest(
                    subtitle_queue,
                    subtitle_item,
                    log,
                    "subtitle_queue",
                )
                runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=True,
                    subtitle_suppressed_reason="",
                )
            else:
                metrics.increment("translation.empty")
                runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=False,
                    subtitle_suppressed_reason="",
                )
            metrics.log_summary_if_due()

        try:
            while not stop_event.is_set():
                if provisional_future is not None and provisional_future.done():
                    provisional_future = None
                if provisional_queue is not None and provisional_future is None:
                    try:
                        request = provisional_queue.get_nowait()
                    except queue.Empty:
                        request = None
                    if isinstance(request, ProvisionalRequest):
                        provisional_future = provisional_executor.submit(
                            translate_provisional, request
                        )
                collect_finished()
                while next_emit_seq in completed:
                    emit_completed(completed.pop(next_emit_seq))
                    next_emit_seq += 1

                if len(pending) >= _MAX_PENDING_TRANSLATIONS:
                    stop_event.wait(_TRANSLATION_LOOP_POLL_SEC)
                    continue

                has_item, item = poll_queue(
                    sentence_queue,
                    stop_event,
                    pause_event,
                    timeout=_TRANSLATION_LOOP_POLL_SEC,
                )
                if has_item:
                    submitted_at = time.monotonic()
                    submitted_at_utc = datetime.now(timezone.utc).isoformat()
                    pending[next_seq] = executor.submit(
                        translate_item,
                        next_seq,
                        item,
                        submitted_at,
                        submitted_at_utc,
                    )
                    next_seq += 1
        finally:
            provisional_executor.shutdown(wait=False, cancel_futures=True)
            # Stop accepting work but let already-submitted translations finish:
            # cancel_futures=True dropped completed-but-unemitted and in-flight
            # subtitles at stop. Drain in order with a bounded wait so shutdown
            # can't hang on a stuck engine call.
            executor.shutdown(wait=False, cancel_futures=False)
            deadline = time.monotonic() + _stop_drain_timeout_sec()
            while True:
                collect_finished()
                while next_emit_seq in completed:
                    emit_completed(completed.pop(next_emit_seq))
                    next_emit_seq += 1
                if not pending and not completed:
                    break
                if time.monotonic() >= deadline:
                    log.warning(
                        "Stop drain timed out with %d pending / %d completed translations",
                        len(pending),
                        len(completed),
                    )
                    for future in pending.values():
                        future.cancel()
                    break
                time.sleep(_TRANSLATION_LOOP_POLL_SEC)
            log.info("Translator stopped")
    return start_daemon_thread("Translator", run)


if __name__ == "__main__":
    translator = Translator()
    tests = [
        ("안녕하세요, 오늘 방송에 오신 걸 환영해요!", False),
        ("진짜 대박이다 ㅋㅋㅋ", False),
        ("지금 게임 하고", True),
    ]
    for text, incomplete in tests:
        result = translator.translate(text, incomplete)
        print(f"{text!r} → {result!r}")
