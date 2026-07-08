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

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "scratch" / "analysis" / "llm_quality"
DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENROUTER_QA_MODEL", "anthropic/claude-sonnet-4.6")
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
        }


def resolve_event_paths(event_args: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in event_args:
        pattern = str(PROJECT_ROOT / raw) if not Path(raw).is_absolute() else raw
        matches = sorted(glob.glob(pattern))
        if matches:
            paths.extend(Path(match) for match in matches)
        else:
            path = Path(raw)
            paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _generic_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
        if profile_matches or text_matches:
            if text_matches:
                matched.append(entry)
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
        before = [
            _compact_context_event(events[i])
            for i in range(max(0, index - context_window), index)
        ]
        after = [
            _compact_context_event(events[i])
            for i in range(index + 1, min(len(events), index + context_window + 1))
        ]
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
            )
        )
    return cases


def build_messages(cases: list[ReviewCase]) -> list[dict[str, str]]:
    payload = {
        "task": "review_ko_to_zh_tw_live_subtitles",
        "project_context": {
            "known_fan_terms": load_fan_terms(),
        },
        "cases": [case.to_prompt_dict() for case in cases],
        "required_output": {
            "type": "json_array",
            "schema": {
                "id": "case id from input",
                "severity": "ok | warn | bad",
                "issue_type": (
                    "no_issue | stt_mishear | mistranslation | name_error | "
                    "glossary_gap | amount_error | unnatural_zh | context_error"
                ),
                "confidence": "number from 0 to 1",
                "suggested_translation": "Traditional Chinese subtitle, empty when no change needed",
                "suggested_correction_rule": "short candidate rule, empty when not applicable",
                "reason_zh": "brief Traditional Chinese reason",
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
        "your job is to find errors worth fixing in code, STT hints, profiles, "
        "or correction glossaries. Focus on real subtitle quality problems: STT "
        "mishears, mistranslations, context misunderstandings, proper-name/"
        "fan-name/platform-term errors, game-term errors, amount/number errors, "
        "speaker/subject mistakes, negation mistakes, and zh-TW that is unnatural "
        "enough to affect understanding. "
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
        "Korean token is an error. "
        "If an unallowed Korean token looks like a proper noun, fan name, title, "
        "or streamer-specific term that should be preserved, use issue_type "
        "glossary_gap and suggest a profile/allowlist rule instead of forcing a "
        "Chinese translation. "
        "Use the surrounding context, but do not invent missing facts. "
        "Return only valid JSON, with no markdown."
    )
    user = (
        "Review these compact runtime cases. Do not rewrite every line; mark ok "
        "when the subtitle is acceptable. Prefer concise Traditional Chinese in "
        "suggestions.\n\n"
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
) -> str:
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://github.com/local/live_translate"),
        "X-Title": os.getenv("OPENROUTER_X_TITLE", "live_translate offline QA"),
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    data = json.loads(raw)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError(f"OpenRouter response has no choices: {raw[:1000]}")
    message = choices[0].get("message", {})
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
    stripped = _strip_json_fence(text)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        array_start = stripped.find("[")
        array_end = stripped.rfind("]")
        object_start = stripped.find("{")
        object_end = stripped.rfind("}")
        if array_start >= 0 and array_end > array_start:
            parsed = json.loads(stripped[array_start: array_end + 1])
        elif object_start >= 0 and object_end > object_start:
            parsed = json.loads(stripped[object_start: object_end + 1])
        else:
            raise

    if isinstance(parsed, dict):
        if isinstance(parsed.get("reviews"), list):
            parsed = parsed["reviews"]
        else:
            parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("LLM review response must be a JSON array or object")
    return [item for item in parsed if isinstance(item, dict)]


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
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for start in range(0, len(cases), batch_size):
        batch = cases[start: start + batch_size]
        content = call_openrouter(
            api_key=api_key,
            model=model,
            messages=build_messages(batch),
            endpoint=endpoint,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        for review in parse_llm_reviews(content):
            reviews.append(normalize_review(review))
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
    return merged


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
        severity = row.get("llm_review", {}).get("severity") if not dry_run else ""
        rank = {"bad": 0, "warn": 1, "ok": 2, "": 3}.get(severity, 3)
        return (rank, -int(row.get("suspicion_score") or 0), row.get("case_id") or "")

    for row in sorted(rows, key=sort_key):
        review = row.get("llm_review", {}) if not dry_run else {}
        heading = row.get("case_id", "")
        if review:
            heading += f" - {review.get('severity')} / {review.get('issue_type')}"
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
            lines.append(f"Reason: {review.get('reason_zh')}")
            if review.get("suggested_translation"):
                lines.append("")
                lines.append(f"Suggested: {review.get('suggested_translation')}")
            if review.get("suggested_correction_rule"):
                lines.append("")
                lines.append(f"Rule: `{review.get('suggested_correction_rule')}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, nargs="+", help="runtime-event JSONL file(s) or glob(s)")
    parser.add_argument("--run-id", action="append", default=None, help="only include this run_id; repeatable")
    parser.add_argument("--mode", choices=("suspicious", "broad"), default="suspicious")
    parser.add_argument("--max-cases", type=int, default=80)
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
    parser.add_argument("--endpoint", default=os.getenv("OPENROUTER_ENDPOINT", DEFAULT_OPENROUTER_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-output-tokens", type=int, default=4000)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--dry-run", action="store_true", help="select cases and write report without API calls")
    args = parser.parse_args(argv)

    paths = resolve_event_paths(args.events)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print(f"Event file(s) not found: {missing}", file=sys.stderr)
        return 2

    events = list(iter_translation_events(paths, set(args.run_id) if args.run_id else None))
    if not events:
        print("No translation events matched.", file=sys.stderr)
        return 2

    cases = select_review_cases(
        events,
        mode=args.mode,
        max_cases=max(0, args.max_cases),
        context_window=max(0, args.context_window),
        control_cases=max(0, args.control_cases),
        min_score=max(0, args.min_score),
        include_filtered=args.include_filtered,
    )

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

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is required unless --dry-run is used.", file=sys.stderr)
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
            endpoint=args.endpoint,
            timeout=args.timeout,
            temperature=args.temperature,
            max_tokens=max(1, args.max_output_tokens),
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rows = merge_reviews(cases, reviews)
    write_jsonl(output_dir / "reviews.jsonl", rows)
    report = build_markdown_report(rows, dry_run=False, model=args.model)
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"reviewed {len(rows)} cases with {args.model}: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
