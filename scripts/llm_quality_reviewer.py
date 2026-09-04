"""LLM-assisted offline translation QA for runtime-event logs.

This tool does not run in the live subtitle path and never edits correction
data. It extracts compact translation cases from runtime_events JSONL, selects
high-signal suspicious cases by deterministic heuristics, then optionally asks
OpenRouter for a structured QA review.

Usage:
    python scripts/llm_quality_reviewer.py --events logs/runtime_events_20260707.jsonl --dry-run
    python scripts/llm_quality_reviewer.py --events logs/runtime_events_20260707.jsonl \
        --model openai/gpt-4.1-mini --max-cases 80
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is present in the app env.
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")

from scripts.suggest_corrections import build_hangul_allowlist
from utils.chatgpt_bundle import bundle_event_paths

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "scratch" / "analysis" / "llm_quality"
DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENROUTER_QA_MODEL", "anthropic/claude-sonnet-4.6")
API_CALL_METRICS: list[dict[str, Any]] = []
CALIBRATION_POPULATION = {"known_good": 0, "known_failure": 0}
_FAN_TERMS_PATH = PROJECT_ROOT / "data" / "fan_terms.json"
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7a3]{2,}")
_LATIN_RE = re.compile(r"[A-Za-z]")
_CHAN_RE = re.compile(r"(?:^|[^A-Za-z])[-\u2010-\u2015\uff0d]?chan(?:[^A-Za-z]|$)", re.IGNORECASE)
_META_RE = re.compile(r"\b(?:as an ai|cannot translate|can't translate|i'm sorry|sorry)\b", re.IGNORECASE)
_QUALITY_FLAG_WEIGHTS = {
    "empty_target": 8,
    "very_short_target": 4,
    "target_meta_leak": 5,
    "target_has_hangul": 4,
    "repetitive_target": 4,
    "unbalanced_brackets": 3,
    "target_has_japanese": 3,
    "low_target_cjk": 1,
    "low_source_hangul": 1,
    "target_high_latin": 1,
}


@dataclass(frozen=True)
class ReviewCase:
    case_id: str
    event_index: int
    event_path: str
    created_at: str
    run_id: str
    sequence_id: Any
    source_text: str
    target_text: str
    profile_id: str
    current_activity: str
    engine: str
    quality_severity: str
    quality_flags: list[str]
    suspicion_score: int
    suspicion_reasons: list[str]
    amount_mismatch_candidate: bool
    source_amount_values: list[Any]
    target_amount_values: list[Any]
    corrections: list[Any]
    target_hangul_terms: list[str]
    allowed_hangul_terms: list[str]
    unallowed_hangul_terms: list[str]
    matched_fan_terms: list[dict[str, Any]]
    context_before: list[dict[str, Any]]
    context_after: list[dict[str, Any]]
    source_utterance_ids: list[str]
    evidence_source_utterance_ids: list[str]
    profile_generation: int | None
    profile_cache_identity: str
    profile_evidence_source: str
    activity_id: str
    activity_kind: str
    activity_source: str
    history_cohort_id: str
    history_candidate_count: int
    history_cross_cohort_excluded_count: int
    history_reconstruction: str
    model: str
    route_id: str
    prompt_version: str
    result_source: str
    cache_status: str
    attempts: list[dict[str, Any]]
    subtitle_emitted: bool | None
    subtitle_suppressed_reason: str
    deterministic_failures: list[str]
    calibration_label: str
    calibration_pair_id: str

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "context_before": self.context_before,
            "source": self.source_text,
            "translation": self.target_text,
            "context_after": self.context_after,
            "metadata": {
                "created_at": self.created_at,
                "run_id": self.run_id,
                "sequence_id": self.sequence_id,
                "profile_id": self.profile_id,
                "current_activity": self.current_activity,
                "engine": self.engine,
                "quality_severity": self.quality_severity,
                "quality_flags": self.quality_flags,
                "suspicion_score": self.suspicion_score,
                "suspicion_reasons": self.suspicion_reasons,
                "amount_mismatch_candidate": self.amount_mismatch_candidate,
                "source_amount_values": self.source_amount_values,
                "target_amount_values": self.target_amount_values,
                "corrections": self.corrections,
                "target_hangul_terms": self.target_hangul_terms,
                "allowed_hangul_terms": self.allowed_hangul_terms,
                "unallowed_hangul_terms": self.unallowed_hangul_terms,
                "matched_fan_terms": self.matched_fan_terms,
                "source_utterance_ids": self.source_utterance_ids,
                "evidence_source_utterance_ids": self.evidence_source_utterance_ids,
                "profile_generation": self.profile_generation,
                "profile_cache_identity": self.profile_cache_identity,
                "profile_evidence_source": self.profile_evidence_source,
                "activity_id": self.activity_id,
                "activity_kind": self.activity_kind,
                "activity_source": self.activity_source,
                "history_cohort_id": self.history_cohort_id,
                "history_candidate_count": self.history_candidate_count,
                "history_cross_cohort_excluded_count": self.history_cross_cohort_excluded_count,
                "history_reconstruction": self.history_reconstruction,
                "provider": {
                    "engine": self.engine,
                    "model": self.model,
                    "route_id": self.route_id,
                    "prompt_version": self.prompt_version,
                    "result_source": self.result_source,
                    "cache_status": self.cache_status,
                    "attempts": self.attempts,
                },
                "publication": {
                    "subtitle_emitted": self.subtitle_emitted,
                    "subtitle_suppressed_reason": self.subtitle_suppressed_reason,
                },
                "deterministic_failures": self.deterministic_failures,
            },
        }

    def to_output_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "event_index": self.event_index,
            "event_path": self.event_path,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "sequence_id": self.sequence_id,
            "source_text": self.source_text,
            "target_text": self.target_text,
            "profile_id": self.profile_id,
            "current_activity": self.current_activity,
            "engine": self.engine,
            "quality_severity": self.quality_severity,
            "quality_flags": self.quality_flags,
            "suspicion_score": self.suspicion_score,
            "suspicion_reasons": self.suspicion_reasons,
            "amount_mismatch_candidate": self.amount_mismatch_candidate,
            "source_amount_values": self.source_amount_values,
            "target_amount_values": self.target_amount_values,
            "corrections": self.corrections,
            "target_hangul_terms": self.target_hangul_terms,
            "allowed_hangul_terms": self.allowed_hangul_terms,
            "unallowed_hangul_terms": self.unallowed_hangul_terms,
            "matched_fan_terms": self.matched_fan_terms,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "source_utterance_ids": self.source_utterance_ids,
            "evidence_source_utterance_ids": self.evidence_source_utterance_ids,
            "profile_generation": self.profile_generation,
            "profile_cache_identity": self.profile_cache_identity,
            "profile_evidence_source": self.profile_evidence_source,
            "activity_id": self.activity_id,
            "activity_kind": self.activity_kind,
            "activity_source": self.activity_source,
            "history_cohort_id": self.history_cohort_id,
            "history_candidate_count": self.history_candidate_count,
            "history_cross_cohort_excluded_count": self.history_cross_cohort_excluded_count,
            "history_reconstruction": self.history_reconstruction,
            "model": self.model,
            "route_id": self.route_id,
            "prompt_version": self.prompt_version,
            "result_source": self.result_source,
            "cache_status": self.cache_status,
            "attempts": self.attempts,
            "subtitle_emitted": self.subtitle_emitted,
            "subtitle_suppressed_reason": self.subtitle_suppressed_reason,
            "deterministic_failures": self.deterministic_failures,
            "calibration_label": self.calibration_label,
            "calibration_pair_id": self.calibration_pair_id,
        }


def resolve_event_paths(event_args: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in event_args:
        pattern = str(PROJECT_ROOT / raw) if not Path(raw).is_absolute() else raw
        matches = sorted(glob.glob(pattern))
        if matches:
            for match in matches:
                path = Path(match)
                paths.extend(bundle_event_paths(path) if path.is_dir() else [path])
        else:
            path = Path(raw)
            path = path if path.is_absolute() else PROJECT_ROOT / path
            paths.extend(bundle_event_paths(path) if path.is_dir() else [path])
    return paths


def iter_translation_events(paths: list[Path], run_ids: set[str] | None = None):
    for path in paths:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event_type") != "translation":
                    continue
                if run_ids and event.get("run_id") not in run_ids:
                    continue
                event = event.copy()
                event["_event_path"] = str(path)
                event["_line_number"] = line_number
                yield event


def load_calibration_events(paths: list[Path]) -> list[dict[str, Any]]:
    """Create blinded known-good/known-failure contrasts from reviewed fixtures."""
    events: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"calibration file must be a JSON list: {path}")
        for row in payload:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source_text") or "")
            reference = str(row.get("reference_output") or "")
            current = str(row.get("current_output") or "")
            if not source or not reference or not current:
                continue
            # Only bounded assertion rows are defensible known failures. The
            # remaining references are smoke/reference material, not labels.
            if not (row.get("expected_terms") or row.get("forbidden_terms")):
                continue
            runtime_ref = next(
                (item for item in row.get("runtime_refs", []) if isinstance(item, dict)),
                {},
            )
            base = {
                "event_type": "translation",
                "created_at": "",
                "run_id": str(runtime_ref.get("run_id") or "calibration"),
                "profile_id": str(runtime_ref.get("profile_id") or ""),
                "current_activity": str(row.get("current_activity") or ""),
                "status": "success",
                "quality_severity": "ok",
                "quality_flags": [],
                "source_text": source,
                "engine": "frozen_fixture",
                "model": "human_reference_or_captured_production",
                "subtitle_emitted": True,
                "history_candidate_count": 0,
            }
            pair_id = str(row.get("id") or f"pair-{len(events)}")
            events.append({
                **base,
                "sequence_id": len(events),
                "target_text": reference,
                "_calibration_label": "known_good",
                "_calibration_pair_id": pair_id,
            })
            events.append({
                **base,
                "sequence_id": len(events),
                "target_text": current,
                "_calibration_label": "known_failure",
                "_calibration_pair_id": pair_id,
            })
    return events


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _generic_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_attempts(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain reconstructable provider evidence without transport/error prose."""
    rows: list[dict[str, Any]] = []
    for attempt in _generic_list(event.get("attempts")):
        if not isinstance(attempt, dict):
            continue
        guard = attempt.get("output_guard")
        bounded_guard: dict[str, Any] = {}
        if isinstance(guard, dict):
            for key in (
                "candidate_raw_output", "candidate_output", "candidate_corrections",
                "candidate_quality_flags", "candidate_quality_classifications", "reason",
            ):
                if key in guard:
                    bounded_guard[key] = guard[key]
        rows.append({
            "chain_attempt_index": attempt.get("chain_attempt_index"),
            "phase": str(attempt.get("phase") or ""),
            "engine": str(attempt.get("engine") or ""),
            "model": str(attempt.get("model") or ""),
            "route_id": str(attempt.get("route_id") or ""),
            "status": str(attempt.get("status") or ""),
            "selected_for_output": bool(attempt.get("selected_for_output")),
            "token_prompt": _optional_int(attempt.get("token_prompt")),
            "token_output": _optional_int(attempt.get("token_output")),
            "api_cost_usd": attempt.get("api_cost_usd"),
            "output_guard": bounded_guard,
        })
    return rows


def deterministic_failures(event: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if event.get("status") != "success":
        failures.append("publication_status")
    if not str(event.get("target_text") or "").strip():
        failures.append("empty_target")
    if event.get("subtitle_emitted") is False:
        failures.append("subtitle_not_emitted")
    if event.get("amount_mismatch_candidate"):
        failures.append("number_or_value_mismatch_candidate")
    obligation = event.get("canonical_obligation_evaluation")
    if isinstance(obligation, dict) and obligation.get("passed") is False:
        failures.append("canonical_obligation_missing")
    for classification in _string_list(event.get("quality_classifications")):
        if classification in {"target_has_unexpected_hangul", "target_has_japanese"}:
            failures.append(classification)
    attempts = _generic_list(event.get("attempts"))
    if any(
        isinstance(attempt, dict)
        and attempt.get("selected_for_output")
        and attempt.get("status") not in {"success", None, ""}
        for attempt in attempts
    ):
        failures.append("selected_attempt_not_successful")
    return list(dict.fromkeys(failures))


def load_fan_terms(path: Path = _FAN_TERMS_PATH) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("fan_terms") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term") or "").strip()
        if not term:
            continue
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        normalized.append(
            {
                "profile_id": str(entry.get("profile_id") or ""),
                "group": str(entry.get("group") or ""),
                "streamer": str(entry.get("streamer") or ""),
                "fandom_of": str(entry.get("fandom_of") or ""),
                "term": term,
                "rendering": str(entry.get("rendering") or term),
                "aliases": [str(alias) for alias in aliases if str(alias)],
                "notes": str(entry.get("notes") or ""),
            }
        )
    return normalized


def _fan_term_search_values(entry: dict[str, Any]) -> list[str]:
    values = [str(entry.get("term") or ""), str(entry.get("rendering") or "")]
    aliases = entry.get("aliases", [])
    if isinstance(aliases, list):
        values.extend(str(alias) for alias in aliases)
    return [value for value in values if value]


def matching_fan_terms(
    source: str,
    target: str,
    profile_id: str,
    fan_terms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    haystack = f"{source}\n{target}"
    matched: list[dict[str, Any]] = []
    for entry in fan_terms:
        entry_profile = str(entry.get("profile_id") or "")
        profile_matches = not entry_profile or entry_profile == profile_id
        text_matches = any(value in haystack for value in _fan_term_search_values(entry))
        if text_matches:
            matched.append({
                **entry,
                "active_for_effective_profile": profile_matches,
            })
    return matched


def fixed_term_rendering_misses(
    source: str,
    target: str,
    profile_id: str,
    fan_terms: list[dict[str, Any]],
) -> list[str]:
    misses: list[str] = []
    for entry in fan_terms:
        entry_profile = str(entry.get("profile_id") or "")
        if entry_profile and entry_profile != profile_id:
            continue
        term = str(entry.get("term") or "")
        rendering = str(entry.get("rendering") or term)
        if not term or not rendering:
            continue
        aliases = entry.get("aliases", [])
        source_values = [term]
        if isinstance(aliases, list):
            source_values.extend(str(alias) for alias in aliases if str(alias))
        if not any(value and value in source for value in source_values):
            continue

        accepted_values = {term, rendering}
        if not any(value and value in target for value in accepted_values):
            misses.append(f"{term}->{rendering}")
    return misses


def _text_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _hangul_terms(text: str) -> list[str]:
    return list(dict.fromkeys(_HANGUL_RUN_RE.findall(text or "")))


def _is_allowed_hangul(token: str, allowlist: frozenset[str], suffixes: frozenset[str]) -> bool:
    if token in allowlist:
        return True
    for suffix in suffixes:
        if suffix and token.endswith(suffix) and token[: -len(suffix)] in allowlist:
            return True
    return any(token in term for term in allowlist if len(term) > len(token))


def _hangul_term_groups(
    text: str,
    allowlist: frozenset[str],
    suffixes: frozenset[str],
) -> tuple[list[str], list[str], list[str]]:
    terms = _hangul_terms(text)
    allowed = [term for term in terms if _is_allowed_hangul(term, allowlist, suffixes)]
    unallowed = [term for term in terms if term not in allowed]
    return terms, allowed, unallowed


def suspicion_score(
    event: dict[str, Any],
    *,
    allowlist: frozenset[str] = frozenset(),
    suffixes: frozenset[str] = frozenset(),
    fan_terms: list[dict[str, Any]] | None = None,
) -> tuple[int, list[str]]:
    source = str(event.get("source_text") or "")
    target = str(event.get("target_text") or "")
    profile_id = str(event.get("profile_id") or "")
    flags = _string_list(event.get("quality_flags"))
    _terms, _allowed_terms, unallowed_hangul_terms = _hangul_term_groups(target, allowlist, suffixes)
    reasons: list[str] = []
    score = 0

    status = str(event.get("status") or "")
    if status != "success":
        score += 4
        reasons.append(f"status:{status or 'unknown'}")
    if not target:
        score += 8
        reasons.append("empty_target")

    severity = str(event.get("quality_severity") or "ok")
    if severity == "bad":
        score += 6
        reasons.append("quality_severity:bad")
    elif severity == "warn":
        score += 3
        reasons.append("quality_severity:warn")
    if flags:
        flag_score = 0
        for flag in flags:
            if flag == "target_has_hangul" and not unallowed_hangul_terms:
                continue
            flag_score += _QUALITY_FLAG_WEIGHTS.get(flag, 2)
        if flag_score:
            score += min(8, flag_score)
            reasons.append("quality_flags:" + ",".join(flags[:4]))

    if bool(event.get("amount_mismatch_candidate")):
        score += 7
        reasons.append("amount_mismatch_candidate")
    elif event.get("source_amount_values") and not event.get("target_amount_values"):
        score += 4
        reasons.append("source_amount_missing_in_target")

    corrections = _generic_list(event.get("corrections"))
    correction_count = int(event.get("correction_count") or len(corrections))
    if correction_count:
        score += min(4, 1 + correction_count)
        reasons.append(f"corrections_applied:{correction_count}")

    if target and unallowed_hangul_terms:
        score += 4
        reasons.append("target_contains_unallowed_hangul:" + ",".join(unallowed_hangul_terms[:3]))
    if target and _CHAN_RE.search(target):
        score += 5
        reasons.append("target_contains_chan_shape")
    if "Chaenna們" in target or "Chaenna们" in target:
        score += 5
        reasons.append("fan_name_plural_shape")
    if target and _META_RE.search(target):
        score += 6
        reasons.append("possible_llm_meta_refusal")

    rendering_misses = fixed_term_rendering_misses(source, target, profile_id, fan_terms or [])
    if rendering_misses:
        score += min(8, 4 * len(rendering_misses))
        reasons.append("fixed_term_rendering_missing:" + ",".join(rendering_misses[:3]))

    if "낳" in source and ("生下" in target or "生出" in target or "生孩子" in target):
        score += 5
        reasons.append("possible_recovery_birth_confusion")
    if "배고파" in source and ("裝作很餓" in target or "做出餓了" in target):
        score += 4
        reasons.append("possible_hungry_gesture_misread")

    source_len = _text_len(source)
    target_len = _text_len(target)
    if source_len >= 8 and target_len:
        ratio = target_len / max(1, source_len)
        if ratio < 0.25:
            score += 3
            reasons.append("target_very_short")
        elif ratio > 4.0:
            score += 2
            reasons.append("target_very_long")

    if target:
        latin_count = len(_LATIN_RE.findall(target))
        if latin_count >= 12 and latin_count / max(1, target_len) > 0.35:
            score += 1
            reasons.append("high_latin_target")

    return score, reasons


def _compact_context_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence_id": event.get("sequence_id"),
        "source": str(event.get("source_text") or ""),
        "translation": str(event.get("target_text") or ""),
    }


def _production_history_context(
    events: list[dict[str, Any]], index: int, limit: int
) -> tuple[list[dict[str, Any]], str]:
    """Best-effort reconstruction from earlier published same-cohort rows only.

    Runtime telemetry does not retain the byte-exact provider history payload.
    The returned label makes that limitation explicit and prevents future
    subtitles from leaking into review context.
    """
    if limit <= 0:
        return [], "not_requested"
    current = events[index]
    available = max(0, _optional_int(current.get("history_candidate_count")) or 0)
    if available <= 0:
        return [], "telemetry_reports_no_history"
    cohort = str(current.get("history_cohort_id") or "")
    profile_identity = str(
        current.get("history_profile_id")
        or current.get("profile_cache_identity")
        or current.get("profile_id")
        or ""
    )
    if not cohort and not profile_identity:
        return [], "insufficient_cohort_identity"
    candidates: list[dict[str, Any]] = []
    for prior in reversed(events[:index]):
        if prior.get("run_id") != current.get("run_id"):
            continue
        prior_cohort = str(prior.get("history_cohort_id") or "")
        prior_profile = str(
            prior.get("history_profile_id")
            or prior.get("profile_cache_identity")
            or prior.get("profile_id")
            or ""
        )
        if cohort and prior_cohort != cohort:
            continue
        if not cohort and profile_identity and prior_profile != profile_identity:
            continue
        if prior.get("status") != "success" or prior.get("subtitle_emitted") is False:
            continue
        candidates.append(_compact_context_event(prior))
        if len(candidates) >= min(limit, available):
            break
    candidates.reverse()
    return candidates, "approximate_prior_published_same_cohort"


def _even_sample(indices: list[int], limit: int) -> list[int]:
    if limit <= 0 or not indices:
        return []
    if len(indices) <= limit:
        return indices
    if limit == 1:
        return [indices[0]]
    step = (len(indices) - 1) / (limit - 1)
    selected: list[int] = []
    seen: set[int] = set()
    for offset in range(limit):
        index = indices[round(offset * step)]
        if index not in seen:
            selected.append(index)
            seen.add(index)
    for index in indices:
        if len(selected) >= limit:
            break
        if index not in seen:
            selected.append(index)
            seen.add(index)
    return sorted(selected)


def select_review_cases(
    events: list[dict[str, Any]],
    *,
    mode: str = "suspicious",
    max_cases: int = 80,
    context_window: int = 1,
    control_cases: int = 8,
    min_score: int = 1,
    include_filtered: bool = False,
) -> list[ReviewCase]:
    candidate_indices = [
        index for index, event in enumerate(events)
        if include_filtered
        or (
            event.get("status") == "success"
            and bool(str(event.get("target_text") or "").strip())
        )
    ]
    scored: list[tuple[int, int, list[str]]] = []
    allowlist, suffixes = build_hangul_allowlist()
    fan_terms = load_fan_terms()
    for index in candidate_indices:
        event = events[index]
        score, reasons = suspicion_score(
            event,
            allowlist=allowlist,
            suffixes=suffixes,
            fan_terms=fan_terms,
        )
        scored.append((index, score, reasons))

    if mode == "broad":
        selected_indices = _even_sample(candidate_indices, max_cases)
    else:
        suspicious = [
            item for item in scored
            if item[1] >= min_score
        ]
        suspicious.sort(key=lambda item: (-item[1], item[0]))
        control_limit = min(max(0, control_cases), max(0, max_cases - 1)) if suspicious else max(0, control_cases)
        selected_indices = [item[0] for item in suspicious[:max(0, max_cases - control_limit)]]
        selected_set = set(selected_indices)
        controls = [
            index for index, score, _reasons in scored
            if score <= 0 and index not in selected_set
            and (events[index].get("source_text") or events[index].get("target_text"))
        ]
        selected_indices.extend(_even_sample(controls, max(0, max_cases - len(selected_indices))))
        if len(selected_indices) < max_cases:
            for index, _score, _reasons in suspicious:
                if len(selected_indices) >= max_cases:
                    break
                if index not in selected_indices:
                    selected_indices.append(index)

    score_by_index = {index: (score, reasons) for index, score, reasons in scored}
    cases: list[ReviewCase] = []
    for position, index in enumerate(selected_indices[:max_cases], start=1):
        event = events[index]
        score, reasons = score_by_index[index]
        target_hangul_terms, allowed_hangul_terms, unallowed_hangul_terms = _hangul_term_groups(
            str(event.get("target_text") or ""),
            allowlist,
            suffixes,
        )
        source_text = str(event.get("source_text") or "")
        target_text = str(event.get("target_text") or "")
        profile_id = str(event.get("profile_id") or "")
        before, history_reconstruction = _production_history_context(
            events, index, context_window
        )
        # Future subtitles were not available to production and must never be
        # supplied as reviewer evidence.
        after: list[dict[str, Any]] = []
        cases.append(
            ReviewCase(
                case_id=f"case_{position:04d}",
                event_index=index,
                event_path=str(event.get("_event_path") or ""),
                created_at=str(event.get("created_at") or ""),
                run_id=str(event.get("run_id") or ""),
                sequence_id=event.get("sequence_id"),
                source_text=source_text,
                target_text=target_text,
                profile_id=profile_id,
                current_activity=str(event.get("current_activity") or ""),
                engine=str(event.get("engine") or ""),
                quality_severity=str(event.get("quality_severity") or ""),
                quality_flags=_string_list(event.get("quality_flags")),
                suspicion_score=score,
                suspicion_reasons=reasons,
                amount_mismatch_candidate=bool(event.get("amount_mismatch_candidate")),
                source_amount_values=_generic_list(event.get("source_amount_values")),
                target_amount_values=_generic_list(event.get("target_amount_values")),
                corrections=_generic_list(event.get("corrections")),
                target_hangul_terms=target_hangul_terms,
                allowed_hangul_terms=allowed_hangul_terms,
                unallowed_hangul_terms=unallowed_hangul_terms,
                matched_fan_terms=matching_fan_terms(
                    source_text,
                    target_text,
                    profile_id,
                    fan_terms,
                ),
                context_before=before,
                context_after=after,
                source_utterance_ids=_string_list(event.get("source_utterance_ids")),
                evidence_source_utterance_ids=_string_list(event.get("evidence_source_utterance_ids")),
                profile_generation=_optional_int(event.get("profile_generation")),
                profile_cache_identity=str(event.get("profile_cache_identity") or ""),
                profile_evidence_source=str(event.get("profile_evidence_source") or ""),
                activity_id=str(event.get("activity_id") or ""),
                activity_kind=str(event.get("activity_kind") or ""),
                activity_source=str(event.get("activity_source") or ""),
                history_cohort_id=str(event.get("history_cohort_id") or ""),
                history_candidate_count=max(0, _optional_int(event.get("history_candidate_count")) or 0),
                history_cross_cohort_excluded_count=max(0, _optional_int(event.get("history_cross_cohort_excluded_count")) or 0),
                history_reconstruction=history_reconstruction,
                model=str(event.get("model") or ""),
                route_id=str(event.get("route_id") or ""),
                prompt_version=str(event.get("prompt_version") or ""),
                result_source=str(event.get("result_source") or ""),
                cache_status=str(event.get("cache_status") or ""),
                attempts=_bounded_attempts(event),
                subtitle_emitted=(event.get("subtitle_emitted") if isinstance(event.get("subtitle_emitted"), bool) else None),
                subtitle_suppressed_reason=str(event.get("subtitle_suppressed_reason") or ""),
                deterministic_failures=deterministic_failures(event),
                calibration_label=str(event.get("_calibration_label") or ""),
                calibration_pair_id=str(event.get("_calibration_pair_id") or ""),
            )
        )
    return cases


def build_messages(cases: list[ReviewCase]) -> list[dict[str, str]]:
    relevant_terms: list[dict[str, Any]] = []
    seen_terms: set[tuple[str, str]] = set()
    for case in cases:
        for term in case.matched_fan_terms:
            identity = (str(term.get("profile_id") or ""), str(term.get("term") or ""))
            if identity not in seen_terms:
                seen_terms.add(identity)
                relevant_terms.append(term)
    payload = {
        "task": "review_ko_to_zh_tw_live_subtitles",
        "project_context": {
            "known_fan_terms": relevant_terms,
        },
        "cases": [case.to_prompt_dict() for case in cases],
        "required_output": {
            "type": "json_object_with_reviews_array",
            "schema": {
                "id": "case id from input",
                "verdict": "ok | suspicious | wrong",
                "severity": "integer 0..3",
                "categories": [
                    "meaning", "context", "role_direction", "number_quantity",
                    "terminology", "entity", "negation_modality", "incomplete_meaning",
                    "naturalness", "source_uncertain", "other",
                ],
                "brief_reason": "short evidence-based Traditional Chinese explanation",
                "source_needs_verification": "boolean",
            },
        },
    }
    system = (
        "You are a QA reviewer for a low-latency Korean live-stream subtitle "
        "translator. The system translates Korean streamer/VTuber live speech "
        "into concise Traditional Chinese for Taiwan viewers. The source may "
        "come from SOOP, CHZZK, YouTube, or legacy Twitch/Korean streaming "
        "contexts, and often includes platform slang, streamer names, fan names, "
        "donations, chat reactions, game titles, game mechanics, memes, and "
        "rapid casual speech. Your job is not to make every subtitle prettier; "
        "your job is to judge whether the final Traditional Chinese preserves "
        "the defensible meaning of the Korean source with only the evidence in "
        "the payload. Do not retranslate the sentence and do not propose code "
        "changes. Fluent-looking Chinese can still be semantically wrong. Focus "
        "on word sense, context, roles/direction, entity identity, numbers, "
        "negation/modality, omitted meaning, and meaning-affecting naturalness. "
        "Platform names and Korean platform terms may need exact handling: SOOP "
        "may appear as SOOP/숲; CHZZK may appear as CHZZK/치지직. Do not confuse "
        "platform names with ordinary words, and suggest glossary_gap when a "
        "platform/streamer/fan/game term should be preserved or standardized. "
        "Some Korean tokens are intentionally preserved in this project; when a "
        "case metadata field allowed_hangul_terms contains a token, do not mark "
        "the case wrong solely because that token remains Korean. "
        "The payload includes project_context.known_fan_terms and per-case "
        "metadata.matched_fan_terms. Use those fields to understand which group, "
        "streamer, or fandom a fan name belongs to before deciding whether a "
        "Korean token is an error. A matched term is an active glossary rule only "
        "when active_for_effective_profile is true; false means cross-profile "
        "reference evidence and must not be treated as a production obligation. "
        "If an unallowed Korean token looks like a proper noun, fan name, title, "
        "or streamer-specific term that should be preserved, use issue_type "
        "glossary_gap and suggest a profile/allowlist rule instead of forcing a "
        "Chinese translation. "
        "The context_before field contains only earlier published same-cohort "
        "rows reconstructed from telemetry; context_after is deliberately empty "
        "because future subtitles were unavailable to production. Attempt output "
        "guards may expose actual raw provider candidates and correction traces. "
        "Do not invent missing prompt/history/profile facts. If source truth is "
        "not defensible without listening to audio, use category source_uncertain, "
        "set source_needs_verification true, and do not blame translation. Mark "
        "ok when meaning is defensibly preserved. Return exactly one object per "
        "case ID, in input order, wrapped as {\"reviews\":[...]}; valid JSON only, "
        "no markdown or chain-of-thought."
    )
    user = (
        "Review these reconstructed runtime cases for semantic fidelity. Keep "
        "brief_reason non-empty, short, and evidence-based for every verdict, "
        "including ok.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def call_openrouter(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    endpoint: str = DEFAULT_OPENROUTER_ENDPOINT,
    timeout: float = 60.0,
    temperature: float = 0.1,
    max_tokens: int = 4000,
    _rate_retry: int = 1,
) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if "api.groq.com" in endpoint:
        body["reasoning_effort"] = "low"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "live_translate/semantic-review",
        "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://github.com/local/live_translate"),
        "X-Title": os.getenv("OPENROUTER_X_TITLE", "live_translate offline QA"),
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 429 and _rate_retry > 0:
            match = re.search(r"try again in ([0-9.]+)s", error_body, re.IGNORECASE)
            delay = min(60.0, max(1.0, float(match.group(1)) + 1.0)) if match else 30.0
            time.sleep(delay)
            return call_openrouter(
                api_key=api_key,
                model=model,
                messages=messages,
                endpoint=endpoint,
                timeout=timeout,
                temperature=temperature,
                max_tokens=max_tokens,
                _rate_retry=_rate_retry - 1,
            )
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    data = json.loads(raw)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError(f"OpenRouter response has no choices: {raw[:1000]}")
    message = choices[0].get("message", {})
    usage = data.get("usage") if isinstance(data, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    API_CALL_METRICS.append({
        "model": model,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
        "prompt_tokens": _optional_int(usage.get("prompt_tokens")),
        "completion_tokens": _optional_int(usage.get("completion_tokens")),
        "total_tokens": _optional_int(usage.get("total_tokens")),
        "cost_usd": usage.get("cost"),
    })
    content = message.get("content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def parse_llm_reviews(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        raise ValueError("semantic review response must not use markdown fences")
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict) or set(parsed) != {"reviews"}:
        raise ValueError("semantic review response must be exactly {'reviews': [...]}")
    reviews = parsed["reviews"]
    if not isinstance(reviews, list) or any(not isinstance(item, dict) for item in reviews):
        raise ValueError("semantic review reviews must be an array of objects")
    return reviews


def _float_between_zero_and_one(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def normalize_review(review: dict[str, Any]) -> dict[str, Any]:
    severity = str(review.get("severity") or "warn").strip().lower()
    if severity not in {"ok", "warn", "bad"}:
        severity = "warn"
    issue_type = str(review.get("issue_type") or "no_issue").strip()
    allowed_issue_types = {
        "no_issue",
        "stt_mishear",
        "mistranslation",
        "name_error",
        "glossary_gap",
        "amount_error",
        "unnatural_zh",
        "context_error",
    }
    if issue_type not in allowed_issue_types:
        issue_type = "mistranslation" if severity in {"warn", "bad"} else "no_issue"
    return {
        "id": str(review.get("id") or ""),
        "severity": severity,
        "issue_type": issue_type,
        "confidence": _float_between_zero_and_one(review.get("confidence")),
        "suggested_translation": str(review.get("suggested_translation") or ""),
        "suggested_correction_rule": str(review.get("suggested_correction_rule") or ""),
        "reason_zh": str(review.get("reason_zh") or ""),
    }


_SEMANTIC_VERDICTS = {"ok", "suspicious", "wrong"}
_SEMANTIC_CATEGORIES = {
    "meaning", "context", "role_direction", "number_quantity", "terminology",
    "entity", "negation_modality", "incomplete_meaning", "naturalness",
    "source_uncertain", "other",
}
_SEMANTIC_REVIEW_KEYS = {
    "id", "verdict", "severity", "categories", "brief_reason",
    "source_needs_verification",
}


def _model_family(value: str) -> str:
    text = value.casefold()
    for family in ("deepseek", "qwen", "gpt-oss", "claude", "gemini", "llama"):
        if family in text:
            return family
    return text.rsplit("/", 1)[-1].split(":", 1)[0]


def validate_reviewer_independence(cases: list[ReviewCase], reviewer_model: str) -> None:
    reviewer_family = _model_family(reviewer_model)
    if "deepseek" in reviewer_model.casefold():
        raise ValueError("DeepSeek reviewers are excluded from semantic triage")
    conflicts = sorted({
        case.case_id
        for case in cases
        if case.model and _model_family(case.model) == reviewer_family
    })
    if conflicts:
        raise ValueError(
            f"reviewer model family matches candidate producer for cases: {conflicts}"
        )


def validate_semantic_reviews(
    raw_reviews: list[dict[str, Any]], cases: list[ReviewCase]
) -> list[dict[str, Any]]:
    """Fail closed on malformed, incomplete, duplicate, or contradictory review output."""
    expected = [case.case_id for case in cases]
    if len(raw_reviews) != len(expected):
        raise ValueError(
            f"semantic review coverage mismatch: expected={len(expected)} actual={len(raw_reviews)}"
        )
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for position, review in enumerate(raw_reviews):
        if set(review) != _SEMANTIC_REVIEW_KEYS:
            raise ValueError(f"semantic review keys invalid at index {position}: {sorted(review)}")
        case_id = review.get("id")
        if case_id != expected[position] or case_id in seen:
            raise ValueError(f"semantic review id/order mismatch at index {position}: {case_id!r}")
        seen.add(str(case_id))
        verdict = review.get("verdict")
        severity = review.get("severity")
        categories = review.get("categories")
        reason = review.get("brief_reason")
        verify = review.get("source_needs_verification")
        if verdict not in _SEMANTIC_VERDICTS:
            raise ValueError(f"invalid semantic verdict for {case_id}: {verdict!r}")
        if isinstance(severity, bool) or not isinstance(severity, int) or not 0 <= severity <= 3:
            raise ValueError(f"invalid semantic severity for {case_id}: {severity!r}")
        if not isinstance(categories, list) or any(
            not isinstance(item, str) or item not in _SEMANTIC_CATEGORIES
            for item in categories
        ) or len(categories) != len(set(categories)):
            raise ValueError(f"invalid semantic categories for {case_id}")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
            raise ValueError(f"invalid semantic reason for {case_id}")
        if not isinstance(verify, bool):
            raise ValueError(f"invalid source verification flag for {case_id}")
        if verdict == "ok" and (severity != 0 or categories or verify):
            raise ValueError(f"contradictory ok review for {case_id}")
        if verdict != "ok" and severity == 0:
            raise ValueError(f"non-ok review requires severity for {case_id}")
        validated.append(dict(review))
    return validated


def review_cases_with_openrouter(
    cases: list[ReviewCase],
    *,
    api_key: str,
    model: str,
    batch_size: int,
    endpoint: str,
    timeout: float,
    temperature: float,
    max_tokens: int,
    inter_call_gap_sec: float = 0.0,
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    validate_reviewer_independence(cases, model)
    groups = [cases]
    if any(case.calibration_label for case in cases):
        # Never expose a known-good reference beside its paired captured output.
        groups = [
            [case for case in cases if case.calibration_label == label]
            for label in ("known_good", "known_failure")
        ]
    for group in groups:
        for start in range(0, len(group), batch_size):
            batch = group[start: start + batch_size]
            if not batch:
                continue
            last_error: Exception | None = None
            for schema_attempt in range(2):
                messages = build_messages(batch)
                if schema_attempt:
                    messages[0]["content"] += (
                        " Previous output failed the declared schema. Recheck every "
                        "invariant, especially: ok requires severity=0, no categories, "
                        "and source_needs_verification=false."
                    )
                content = call_openrouter(
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    endpoint=endpoint,
                    timeout=timeout,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                try:
                    parsed = parse_llm_reviews(content)
                    reviews.extend(validate_semantic_reviews(parsed, batch))
                    if API_CALL_METRICS:
                        API_CALL_METRICS[-1]["schema_retry"] = bool(schema_attempt)
                    last_error = None
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if API_CALL_METRICS:
                        API_CALL_METRICS[-1]["schema_validation_failed"] = True
            if last_error is not None:
                raise last_error
            if inter_call_gap_sec > 0 and (
                group is not groups[-1] or start + batch_size < len(group)
            ):
                time.sleep(inter_call_gap_sec)
    return reviews


def merge_reviews(cases: list[ReviewCase], reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {review.get("id"): review for review in reviews}
    merged: list[dict[str, Any]] = []
    for case in cases:
        item = case.to_output_dict()
        review = by_id.get(case.case_id)
        if review is None:
            item["review_status"] = "missing"
            item["llm_review"] = {}
        else:
            item["review_status"] = "reviewed"
            item["llm_review"] = review
        merged.append(item)
    severity_order = {"wrong": 2, "suspicious": 1, "ok": 0}
    merged.sort(key=lambda row: (
        -int(row.get("llm_review", {}).get("severity") or 0),
        -severity_order.get(str(row.get("llm_review", {}).get("verdict") or ""), -1),
        -len(row.get("deterministic_failures") or []),
        -int(row.get("suspicion_score") or 0),
        str(row.get("run_id") or ""),
        int(row.get("sequence_id") or 0),
    ))
    for rank, item in enumerate(merged, start=1):
        item["rank"] = rank
    return merged


def build_run_summary(
    rows: list[dict[str, Any]], model: str, provider: str = "openrouter"
) -> dict[str, Any]:
    verdicts = Counter(
        str(row.get("llm_review", {}).get("verdict") or "missing") for row in rows
    )
    categories = Counter(
        category
        for row in rows
        for category in row.get("llm_review", {}).get("categories", [])
    )
    prompt_tokens = sum(metric.get("prompt_tokens") or 0 for metric in API_CALL_METRICS)
    completion_tokens = sum(metric.get("completion_tokens") or 0 for metric in API_CALL_METRICS)
    costs = [metric.get("cost_usd") for metric in API_CALL_METRICS]
    numeric_costs = [float(value) for value in costs if isinstance(value, (int, float))]
    latencies = [float(metric["latency_ms"]) for metric in API_CALL_METRICS]
    result = {
        "schema_version": 1,
        "reviewer_model": model,
        "reviewer_provider": provider,
        "cases_reviewed": len(rows),
        "run_ids": sorted({str(row.get("run_id") or "") for row in rows}),
        "verdict_counts": dict(verdicts),
        "category_counts": dict(categories),
        "suspicious_rate": round(
            sum(verdicts[key] for key in ("suspicious", "wrong")) / max(1, len(rows)), 4
        ),
        "source_verification_required": sum(
            bool(row.get("llm_review", {}).get("source_needs_verification")) for row in rows
        ),
        "deterministic_failure_cases": sum(bool(row.get("deterministic_failures")) for row in rows),
        "review_calls": len(API_CALL_METRICS),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(sum(numeric_costs), 8) if numeric_costs else None,
        "review_latency_ms_total": round(sum(latencies), 2),
        "review_latency_ms_mean": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "api_calls": list(API_CALL_METRICS),
    }
    labeled = [row for row in rows if row.get("calibration_label")]
    if labeled:
        positives = [row for row in labeled if row["calibration_label"] == "known_failure"]
        negatives = [row for row in labeled if row["calibration_label"] == "known_good"]
        population_positives = CALIBRATION_POPULATION["known_failure"] or len(positives)
        population_negatives = CALIBRATION_POPULATION["known_good"] or len(negatives)
        is_flagged = lambda row: row.get("llm_review", {}).get("verdict") in {"suspicious", "wrong"}
        result["calibration"] = {
            "known_failure_count": population_positives,
            "known_good_count": population_negatives,
            "reviewed_failure_count": len(positives),
            "reviewed_good_count": len(negatives),
            "selection_coverage": round(len(labeled) / max(1, population_positives + population_negatives), 4),
            "known_failure_recall": round(sum(is_flagged(row) for row in positives) / max(1, population_positives), 4),
            "known_good_false_positive_rate": round(sum(is_flagged(row) for row in negatives) / max(1, population_negatives), 4),
            "top_k": {},
        }
        for k in (10, 20, 40):
            selected = labeled[: min(k, len(labeled))]
            failures = sum(row.get("calibration_label") == "known_failure" for row in selected)
            result["calibration"]["top_k"][str(k)] = {
                "reviewed": len(selected),
                "precision": round(failures / max(1, len(selected)), 4),
                "recall": round(failures / max(1, population_positives), 4),
            }
        by_pair: dict[str, dict[str, int]] = {}
        for row in labeled:
            pair_id = str(row.get("calibration_pair_id") or "")
            if pair_id:
                by_pair.setdefault(pair_id, {})[str(row["calibration_label"])] = int(row.get("rank") or 0)
        complete_pairs = [pair for pair in by_pair.values() if set(pair) == {"known_good", "known_failure"}]
        result["calibration"]["pairwise_discrimination"] = {
            "complete_pairs": len(complete_pairs),
            "failure_ranked_above_reference": sum(
                pair["known_failure"] < pair["known_good"] for pair in complete_pairs
            ),
            "rate": round(
                sum(pair["known_failure"] < pair["known_good"] for pair in complete_pairs)
                / max(1, len(complete_pairs)), 4
            ),
        }
    return result


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_markdown_report(rows: list[dict[str, Any]], *, dry_run: bool, model: str) -> str:
    lines: list[str] = []
    title = "LLM Quality Reviewer Dry Run" if dry_run else "LLM Quality Reviewer Report"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- model: `{model}`")
    lines.append(f"- cases: {len(rows)}")
    if dry_run:
        lines.append("- status: no API call was made")
    lines.append("")

    def sort_key(row: dict[str, Any]):
        severity = row.get("llm_review", {}).get("severity") if not dry_run else -1
        return (-int(severity if isinstance(severity, int) else -1), -int(row.get("suspicion_score") or 0), row.get("case_id") or "")

    for row in sorted(rows, key=sort_key):
        review = row.get("llm_review", {}) if not dry_run else {}
        heading = f"{row.get('rank', '-')}. {row.get('case_id', '')}"
        if review:
            heading += f" - {review.get('verdict')} / severity {review.get('severity')}"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"- score: {row.get('suspicion_score')} ({', '.join(row.get('suspicion_reasons') or [])})")
        lines.append(f"- run/seq: `{row.get('run_id')}` / `{row.get('sequence_id')}`")
        if row.get("profile_id") or row.get("current_activity"):
            lines.append(f"- profile/activity: `{row.get('profile_id')}` / `{row.get('current_activity')}`")
        lines.append("")
        lines.append(f"KO: {row.get('source_text')}")
        lines.append("")
        lines.append(f"ZH: {row.get('target_text')}")
        if review:
            lines.append("")
            lines.append(f"Categories: {', '.join(review.get('categories') or [])}")
            lines.append(f"Reason: {review.get('brief_reason')}")
            lines.append(f"Source/STT verification: {review.get('source_needs_verification')}")
        lines.append(f"Deterministic flags: {', '.join(row.get('deterministic_failures') or [])}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--events", nargs="+", help="runtime-event JSONL file(s) or glob(s)")
    source.add_argument(
        "--calibration-cases", nargs="+",
        help="reviewed semantic eval JSON files; reviews bounded reference/current contrasts blindly",
    )
    parser.add_argument("--run-id", action="append", default=None, help="only include this run_id; repeatable")
    parser.add_argument("--mode", choices=("suspicious", "broad"), default="suspicious")
    parser.add_argument("--max-cases", type=int, default=80)
    parser.add_argument("--case-offset", type=int, default=0, help="resume from this stable selected-case offset")
    parser.add_argument("--control-cases", type=int, default=8)
    parser.add_argument("--context-window", type=int, default=1)
    parser.add_argument("--min-score", type=int, default=1)
    parser.add_argument(
        "--include-filtered",
        action="store_true",
        help="also review filtered/empty target events; off by default to avoid spending QA on known junk",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", choices=("openrouter", "groq"), default="openrouter")
    parser.add_argument("--endpoint", default=os.getenv("OPENROUTER_ENDPOINT", DEFAULT_OPENROUTER_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    parser.add_argument("--inter-call-gap-sec", type=float, default=0.0)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--dry-run", action="store_true", help="select cases and write report without API calls")
    args = parser.parse_args(argv)
    API_CALL_METRICS.clear()
    CALIBRATION_POPULATION.update(known_good=0, known_failure=0)

    paths = resolve_event_paths(args.events or args.calibration_cases)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print(f"Event file(s) not found: {missing}", file=sys.stderr)
        return 2

    events = (
        load_calibration_events(paths)
        if args.calibration_cases
        else list(iter_translation_events(paths, set(args.run_id) if args.run_id else None))
    )
    if args.calibration_cases:
        CALIBRATION_POPULATION.update(
            known_good=sum(event.get("_calibration_label") == "known_good" for event in events),
            known_failure=sum(event.get("_calibration_label") == "known_failure" for event in events),
        )
    if not events:
        print("No translation events matched.", file=sys.stderr)
        return 2
    events.sort(key=lambda event: (
        str(event.get("run_id") or ""),
        int(event.get("sequence_id") or 0),
        str(event.get("created_at") or ""),
    ))

    case_offset = max(0, args.case_offset)
    selection_limit = (
        len(events) if args.mode == "broad" and case_offset else max(0, args.max_cases) + case_offset
    )
    cases = select_review_cases(
        events,
        mode=args.mode,
        max_cases=selection_limit,
        context_window=max(0, args.context_window),
        control_cases=max(0, args.control_cases),
        min_score=max(0, args.min_score),
        include_filtered=args.include_filtered,
    )
    cases = cases[case_offset: case_offset + max(0, args.max_cases)]

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / stamp
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_rows = [case.to_output_dict() for case in cases]
    selected_path = output_dir / "selected_cases.jsonl"
    write_jsonl(selected_path, selected_rows)

    if args.dry_run:
        report = build_markdown_report(selected_rows, dry_run=True, model=args.model)
        (output_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"selected {len(cases)} cases (dry run): {output_dir}")
        return 0

    api_key_name = "GROQ_API_KEY_fall_back" if args.provider == "groq" else "OPENROUTER_API_KEY"
    api_key = os.getenv(api_key_name, "").strip()
    if not api_key:
        print(f"{api_key_name} is required unless --dry-run is used.", file=sys.stderr)
        return 2
    if not cases:
        report = build_markdown_report(selected_rows, dry_run=True, model=args.model)
        (output_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"selected 0 cases: {output_dir}")
        return 0

    try:
        reviews = review_cases_with_openrouter(
            cases,
            api_key=api_key,
            model=args.model,
            batch_size=max(1, args.batch_size),
            endpoint=(DEFAULT_GROQ_ENDPOINT if args.provider == "groq" else args.endpoint),
            timeout=args.timeout,
            temperature=args.temperature,
            max_tokens=max(1, args.max_output_tokens),
            inter_call_gap_sec=max(0.0, args.inter_call_gap_sec),
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rows = merge_reviews(cases, reviews)
    write_jsonl(output_dir / "reviews.jsonl", rows)
    summary = build_run_summary(rows, args.model, args.provider)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = build_markdown_report(rows, dry_run=False, model=args.model)
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"reviewed {len(rows)} cases with {args.model}: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
