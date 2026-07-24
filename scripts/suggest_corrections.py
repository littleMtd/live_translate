"""Offline glossary-candidate report generator.

Scans translation runtime events and surfaces neutral *candidates* and counts
for later review. It never writes glossary or correction data.

Detectors:
1. unknown_hangul — prefers runtime's profile-aware
   ``target_unexpected_hangul_spans`` classification. Older events fall back
   to the conservative project-wide keep-list detector.
2. inconsistent_renderings — the same source sentence produced different
   target sentences within the same profile.
3. profile_term_misses — a reviewed profile term occurred in source but its
   required rendering did not occur in target.
4. frequent_corrections — deterministic source/target correction records that
   repeatedly fired.
5. quality summary — warn/bad counts and examples, for context only.

Usage:
    python scripts/suggest_corrections.py --events logs/runtime_events_20260611.jsonl
    python scripts/suggest_corrections.py --events "logs/runtime_events_2026061*.jsonl" \
        --run-id 20260611T035026Z-30856 --min-count 2 \
        --output logs/suggestions.md --json-output logs/glossary_candidates.json
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_DATA_DIR = PROJECT_ROOT / "data"
_HANGUL_RUN_RE = re.compile(r"[가-힣]{2,}")
_MAX_EXAMPLES = 3
_REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Event loading
# ---------------------------------------------------------------------------

def iter_translation_events(paths: list[str], run_ids: set[str] | None = None):
    """Yield translation events from runtime-event JSONL files.

    Tolerates BOMs, blank lines and corrupt lines (live logs get truncated)."""
    # Overlapping globs commonly resolve to the same file. Preserve path order
    # while preventing every count from being multiplied.
    for path in dict.fromkeys(paths):
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
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
                yield event


# ---------------------------------------------------------------------------
# Intentional-keep allowlist
# ---------------------------------------------------------------------------

def _hangul_runs(text: str) -> list[str]:
    return _HANGUL_RUN_RE.findall(text or "")


def build_hangul_allowlist(data_dir: Path = _DATA_DIR) -> tuple[frozenset[str], frozenset[str]]:
    """Return (allowlist, name_suffixes).

    The allowlist is every Hangul run that the project deliberately keeps in
    zh-TW output: profile glossary/example texts, correction canonicals and
    replacement targets, and slang values. Self-updating — extending a
    profile automatically whitelists its terms.
    """
    allow: set[str] = set()

    profiles_path = data_dir / "translation_profiles.json"
    if profiles_path.exists():
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        for variant in profiles.values():
            for text in variant.values():
                allow.update(_hangul_runs(text))

    suffixes: set[str] = set()
    corrections_path = data_dir / "translation_corrections.json"
    if corrections_path.exists():
        corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
        for rule in corrections.get("name_rendering_rules", []):
            allow.update(_hangul_runs(rule.get("canonical", "")))
            for alias in rule.get("source_aliases", []):
                allow.update(_hangul_runs(alias))
        for group in corrections.get("source_aware_target_replacements", []):
            for replacement in group.get("replacements", []):
                allow.update(_hangul_runs(replacement.get("right", "")))
        for groups in corrections.get("profile_source_aware_target_replacements", {}).values():
            for group in groups:
                for replacement in group.get("replacements", []):
                    allow.update(_hangul_runs(replacement.get("right", "")))
        source_norm = corrections.get("source_norm", {})
        for value in source_norm.get("shared", {}).values():
            allow.update(_hangul_runs(value))
        for profile_map in source_norm.get("profiles", {}).values():
            for value in profile_map.values():
                allow.update(_hangul_runs(value))
        suffixes = {s for s in corrections.get("korean_name_suffixes", []) if s}

    slang_path = data_dir / "default_slang.json"
    if slang_path.exists():
        slang = json.loads(slang_path.read_text(encoding="utf-8"))
        if isinstance(slang, dict):
            for key, value in slang.items():
                allow.update(_hangul_runs(str(key)))
                allow.update(_hangul_runs(str(value)))

    return frozenset(allow), frozenset(suffixes)


def _is_allowed(token: str, allowlist: frozenset[str], suffixes: frozenset[str]) -> bool:
    if token in allowlist:
        return True
    # 해둥이들 / 해둥이가 ... -> allowed base + known particle suffix
    for suffix in suffixes:
        if token.endswith(suffix) and token[: -len(suffix)] in allowlist:
            return True
    # A token fully contained in an allowed longer term (e.g. truncated tail)
    return any(token in term for term in allowlist if len(term) > len(token))


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _effective_profile_id(event: dict) -> str:
    """Return the profile that actually governed this event.

    Runtime retains the configured ``profile_id`` even when profile use is
    disabled. Missing ``profile_applied`` is an old-log shape and remains
    legacy-assumed applied; only explicit false disables attribution.
    """
    if event.get("profile_applied") is False:
        return ""
    return str(event.get("profile_id") or "")


@dataclass
class LeakCandidate:
    token: str
    count: int = 0
    profiles: Counter = field(default_factory=Counter)
    examples: list[tuple[str, str]] = field(default_factory=list)  # (source, target)
    source_presence_count: int = 0
    telemetry_event_count: int = 0
    fallback_event_count: int = 0


def find_hangul_leaks(
    events: list[dict],
    allowlist: frozenset[str],
    suffixes: frozenset[str],
) -> list[LeakCandidate]:
    """Return neutral unknown-Hangul candidates.

    New events carry T04's profile-aware classification. The old allowlist
    detector is retained only for logs that predate that field. Counts are
    event counts rather than raw substring occurrences.
    """
    candidates: dict[str, LeakCandidate] = {}
    for event in events:
        if event.get("status") != "success":
            continue
        target = event.get("target_text") or ""
        source = event.get("source_text") or ""
        classified_spans = event.get("target_unexpected_hangul_spans")
        uses_telemetry = isinstance(classified_spans, list)
        if uses_telemetry:
            tokens = {
                str(token).strip()
                for token in classified_spans
                if isinstance(token, str) and str(token).strip()
            }
        else:
            tokens = {
                token
                for token in _hangul_runs(target)
                if not _is_allowed(token, allowlist, suffixes)
            }

        for token in tokens:
            entry = candidates.setdefault(token, LeakCandidate(token))
            entry.count += 1
            entry.profiles[_effective_profile_id(event)] += 1
            if token in source:
                entry.source_presence_count += 1
            if uses_telemetry:
                entry.telemetry_event_count += 1
            else:
                entry.fallback_event_count += 1
            if len(entry.examples) < _MAX_EXAMPLES:
                entry.examples.append((source, target))
    return sorted(candidates.values(), key=lambda c: (-c.count, c.token))


@dataclass
class InconsistencyCandidate:
    source: str
    profile_id: str = ""
    variants: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.variants.values())


def find_inconsistent_translations(events: list[dict]) -> list[InconsistencyCandidate]:
    by_source: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for event in events:
        if event.get("status") != "success":
            continue
        source = " ".join((event.get("source_text") or "").split())
        target = (event.get("target_text") or "").strip()
        if source and target:
            profile_id = _effective_profile_id(event)
            by_source[(profile_id, source)][target] += 1
    results = [
        InconsistencyCandidate(source, profile_id, variants)
        for (profile_id, source), variants in by_source.items()
        if len(variants) >= 2
    ]
    return sorted(results, key=lambda c: (-c.total, c.profile_id, c.source))


def load_fan_terms(data_dir: Path = _DATA_DIR) -> list[dict[str, Any]]:
    """Load reviewed profile-term inventory, tolerating absent/corrupt data."""
    path = data_dir / "fan_terms.json"
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
        profile_id = str(entry.get("profile_id") or "").strip()
        if not term or not profile_id:
            continue
        aliases = entry.get("aliases")
        normalized.append(
            {
                "profile_id": profile_id,
                "term": term,
                "rendering": str(entry.get("rendering") or term).strip(),
                "aliases": [
                    str(alias).strip()
                    for alias in aliases
                    if str(alias).strip()
                ] if isinstance(aliases, list) else [],
                "ambiguous_aliases": [
                    str(alias).strip()
                    for alias in entry.get("ambiguous_aliases", [])
                    if str(alias).strip()
                ] if isinstance(entry.get("ambiguous_aliases"), list) else [],
            }
        )
    return normalized


def _contains_source_form(text: str, form: str) -> bool:
    if not form:
        return False
    if form.isascii() and re.fullmatch(r"[A-Za-z0-9_.:-]+", form):
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(form)}(?![A-Za-z0-9_])",
                text,
                flags=re.IGNORECASE,
            )
        )
    return form in text


@dataclass
class ProfileTermMissCandidate:
    profile_id: str
    term: str
    rendering: str
    count: int = 0
    matched_source_forms: Counter = field(default_factory=Counter)
    examples: list[tuple[str, str]] = field(default_factory=list)


def find_profile_term_misses(
    events: list[dict],
    fan_terms: list[dict[str, Any]],
) -> list[ProfileTermMissCandidate]:
    """Find profile-scoped fixed renderings absent from successful targets."""
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in fan_terms:
        by_profile[str(entry.get("profile_id") or "")].append(entry)

    candidates: dict[tuple[str, str, str], ProfileTermMissCandidate] = {}
    for event in events:
        if event.get("status") != "success":
            continue
        profile_id = _effective_profile_id(event)
        source = str(event.get("source_text") or "")
        target = str(event.get("target_text") or "")
        for entry in by_profile.get(profile_id, []):
            term = str(entry.get("term") or "")
            rendering = str(entry.get("rendering") or term)
            if not term or not rendering or rendering in target:
                continue
            ambiguous_aliases = set(entry.get("ambiguous_aliases", []))
            forms = [
                term,
                *(
                    alias
                    for alias in entry.get("aliases", [])
                    if alias not in ambiguous_aliases
                ),
            ]
            matched = [form for form in forms if _contains_source_form(source, form)]
            if not matched:
                continue
            key = (profile_id, term, rendering)
            candidate = candidates.setdefault(
                key,
                ProfileTermMissCandidate(profile_id, term, rendering),
            )
            candidate.count += 1
            candidate.matched_source_forms.update(set(matched))
            if len(candidate.examples) < _MAX_EXAMPLES:
                candidate.examples.append((source, target))

    return sorted(
        candidates.values(),
        key=lambda c: (-c.count, c.profile_id, c.term),
    )


@dataclass
class CorrectionCandidate:
    stage: str
    rule: str
    before: str
    after: str
    count: int = 0
    profiles: Counter = field(default_factory=Counter)
    examples: list[tuple[str, str]] = field(default_factory=list)


def find_frequent_corrections(events: list[dict]) -> list[CorrectionCandidate]:
    """Aggregate deterministic correction records by event."""
    candidates: dict[tuple[str, str, str, str], CorrectionCandidate] = {}
    for event in events:
        if event.get("status") != "success":
            continue
        corrections = event.get("corrections")
        if not isinstance(corrections, list):
            continue
        seen: set[tuple[str, str, str, str]] = set()
        for raw in corrections:
            if not isinstance(raw, dict):
                continue
            key = tuple(
                str(raw.get(field) or "").strip()
                for field in ("stage", "rule", "before", "after")
            )
            if not key[0] or not key[1] or key in seen:
                continue
            seen.add(key)
            candidate = candidates.setdefault(key, CorrectionCandidate(*key))
            candidate.count += 1
            candidate.profiles[_effective_profile_id(event)] += 1
            if len(candidate.examples) < _MAX_EXAMPLES:
                candidate.examples.append(
                    (
                        str(event.get("source_text") or ""),
                        str(event.get("target_text") or ""),
                    )
                )
    return sorted(
        candidates.values(),
        key=lambda c: (-c.count, c.stage, c.rule, c.before, c.after),
    )


def summarize_quality(events: list[dict]) -> dict:
    severity_counts: Counter = Counter()
    examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for event in events:
        if event.get("status") != "success":
            continue
        severity = event.get("quality_severity") or "unknown"
        severity_counts[severity] += 1
        if severity in ("warn", "bad") and len(examples[severity]) < 5:
            examples[severity].append(
                (event.get("source_text") or "", event.get("target_text") or "")
            )
    return {"counts": dict(severity_counts), "examples": dict(examples)}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _examples_data(examples: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"source": source, "target": target} for source, target in examples]


def _counter_data(counter: Counter) -> dict[str, int]:
    return {str(value): count for value, count in counter.most_common()}


def build_candidate_data(
    events: list[dict],
    *,
    min_count: int = 1,
    data_dir: Path = _DATA_DIR,
) -> dict[str, Any]:
    """Build the stable machine-readable candidate report."""
    if min_count < 1:
        raise ValueError("min_count must be at least 1")

    allowlist, suffixes = build_hangul_allowlist(data_dir)
    unknown_hangul = [
        candidate
        for candidate in find_hangul_leaks(events, allowlist, suffixes)
        if candidate.count >= min_count
    ]
    inconsistencies = [
        candidate
        for candidate in find_inconsistent_translations(events)
        if candidate.total >= max(min_count, 2)
    ]
    profile_misses = [
        candidate
        for candidate in find_profile_term_misses(events, load_fan_terms(data_dir))
        if candidate.count >= min_count
    ]
    corrections = [
        candidate
        for candidate in find_frequent_corrections(events)
        if candidate.count >= min_count
    ]
    quality = summarize_quality(events)

    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "event_counts": {
            "translation": len(events),
            "successful": sum(event.get("status") == "success" for event in events),
        },
        "min_count": min_count,
        "guardrails": {
            "candidate_only": True,
            "mutates_glossary": False,
            "requires_manual_labels": False,
        },
        "evidence": {
            "hangul_allowlist_terms": len(allowlist),
            "unknown_hangul_policy": (
                "runtime_profile_aware_classification_then_legacy_global_allowlist_fallback"
            ),
        },
        "candidates": {
            "unknown_hangul": [
                {
                    "token": candidate.token,
                    "count": candidate.count,
                    "profiles": _counter_data(candidate.profiles),
                    "source_presence_count": candidate.source_presence_count,
                    "telemetry_event_count": candidate.telemetry_event_count,
                    "legacy_fallback_event_count": candidate.fallback_event_count,
                    "examples": _examples_data(candidate.examples),
                }
                for candidate in unknown_hangul
            ],
            "inconsistent_renderings": [
                {
                    "profile_id": candidate.profile_id,
                    "source": candidate.source,
                    "count": candidate.total,
                    "variants": _counter_data(candidate.variants),
                }
                for candidate in inconsistencies
            ],
            "profile_term_misses": [
                {
                    "profile_id": candidate.profile_id,
                    "term": candidate.term,
                    "expected_rendering": candidate.rendering,
                    "count": candidate.count,
                    "matched_source_forms": _counter_data(
                        candidate.matched_source_forms
                    ),
                    "examples": _examples_data(candidate.examples),
                }
                for candidate in profile_misses
            ],
            "frequent_corrections": [
                {
                    "stage": candidate.stage,
                    "rule": candidate.rule,
                    "before": candidate.before,
                    "after": candidate.after,
                    "count": candidate.count,
                    "profiles": _counter_data(candidate.profiles),
                    "examples": _examples_data(candidate.examples),
                }
                for candidate in corrections
            ],
        },
        "quality_summary": quality,
    }


def render_report(data: dict[str, Any]) -> str:
    """Render ``build_candidate_data`` output as a compact Markdown report."""
    event_counts = data["event_counts"]
    evidence = data["evidence"]
    candidates = data["candidates"]
    quality = data["quality_summary"]

    lines: list[str] = [
        "# Automatic Glossary Candidate Report",
        "",
        "本報告只列出 runtime 證據、候選與次數；不會自動修改 glossary，也不要求批次人工標註。",
        "",
        f"Translation events scanned: {event_counts['translation']}",
        f"Successful events: {event_counts['successful']}",
        (
            "Legacy Hangul allowlist terms: "
            f"{evidence['hangul_allowlist_terms']} (profiles/corrections/slang)"
        ),
        "",
    ]

    unknown_hangul = candidates["unknown_hangul"]
    lines.extend([
        f"## 1. Unknown Hangul candidates ({len(unknown_hangul)})",
        "",
        (
            "新事件使用 profile-aware runtime 分類；舊事件才使用全專案 allowlist "
            "fallback。出現在 source 的片段通常較像未登錄專名，不代表翻譯錯誤。"
        ),
        "",
    ])
    if unknown_hangul:
        for candidate in unknown_hangul:
            profiles = ", ".join(
                f"{profile or '(no profile)'}×{count}"
                for profile, count in candidate["profiles"].items()
            )
            lines.append(
                f"### `{candidate['token']}` — {candidate['count']} 次 ({profiles})"
            )
            lines.append(
                "- source 內出現: "
                f"{candidate['source_presence_count']}；runtime telemetry: "
                f"{candidate['telemetry_event_count']}；legacy fallback: "
                f"{candidate['legacy_fallback_event_count']}"
            )
            for example in candidate["examples"]:
                lines.append(f"- KO: {example['source'][:100]}")
                lines.append(f"  ZH: {example['target'][:100]}")
            lines.append("")
    else:
        lines.extend(["(none)", ""])

    inconsistent = candidates["inconsistent_renderings"]
    lines.extend([
        f"## 2. Inconsistent renderings ({len(inconsistent)})",
        "",
        "只比較同一 profile 內完全相同的源句；自然語序差異仍只是候選。",
        "",
    ])
    if inconsistent:
        if len(inconsistent) > 20:
            lines.extend([f"Markdown 僅顯示前 20 筆；JSON 保留全部 {len(inconsistent)} 筆。", ""])
        for candidate in inconsistent[:20]:
            profile = candidate["profile_id"] or "(no profile)"
            lines.append(
                f"### [{profile}] {candidate['source'][:80]} — "
                f"{candidate['count']} 次 / {len(candidate['variants'])} 種"
            )
            for target, count in candidate["variants"].items():
                lines.append(f"- ×{count}: {target[:100]}")
            lines.append("")
    else:
        lines.extend(["(none)", ""])

    profile_misses = candidates["profile_term_misses"]
    lines.extend([
        f"## 3. Profile-term rendering misses ({len(profile_misses)})",
        "",
        "僅檢查 fan_terms.json 中與事件 active profile 相符的固定 rendering。",
        "",
    ])
    if profile_misses:
        for candidate in profile_misses:
            forms = ", ".join(
                f"{form}×{count}"
                for form, count in candidate["matched_source_forms"].items()
            )
            lines.append(
                f"### [{candidate['profile_id']}] `{candidate['term']}` → "
                f"`{candidate['expected_rendering']}` — {candidate['count']} 次"
            )
            lines.append(f"- source forms: {forms}")
            for example in candidate["examples"]:
                lines.append(f"- KO: {example['source'][:100]}")
                lines.append(f"  ZH: {example['target'][:100]}")
            lines.append("")
    else:
        lines.extend(["(none)", ""])

    frequent_corrections = candidates["frequent_corrections"]
    lines.extend([
        f"## 4. Frequently triggered corrections ({len(frequent_corrections)})",
        "",
        "計數代表至少一條 deterministic correction 在該成功事件中觸發。",
        "",
    ])
    if frequent_corrections:
        for candidate in frequent_corrections:
            profiles = ", ".join(
                f"{profile or '(no profile)'}×{count}"
                for profile, count in candidate["profiles"].items()
            )
            lines.append(
                f"### [{candidate['stage']}] `{candidate['rule']}` — "
                f"{candidate['count']} 次 ({profiles})"
            )
            if candidate["before"] or candidate["after"]:
                lines.append(
                    f"- `{candidate['before']}` → `{candidate['after']}`"
                )
            for example in candidate["examples"]:
                lines.append(f"- KO: {example['source'][:100]}")
                lines.append(f"  ZH: {example['target'][:100]}")
            lines.append("")
    else:
        lines.extend(["(none)", ""])

    lines.extend(["## 5. Quality summary", ""])
    for severity, count in sorted(quality["counts"].items()):
        lines.append(f"- {severity}: {count}")
    for severity, pairs in quality["examples"].items():
        lines.append("")
        lines.append(f"### {severity} examples")
        for source, target in pairs:
            lines.append(f"- KO: {source[:80]}")
            lines.append(f"  ZH: {target[:80]}")
    lines.append("")
    return "\n".join(lines)


def build_report(
    events: list[dict],
    *,
    min_count: int = 1,
    data_dir: Path = _DATA_DIR,
) -> str:
    return render_report(
        build_candidate_data(events, min_count=min_count, data_dir=data_dir)
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, nargs="+",
                        help="runtime-event JSONL file(s) or glob(s)")
    parser.add_argument("--run-id", action="append", default=None,
                        help="only include events from this run_id (repeatable)")
    parser.add_argument("--min-count", type=int, default=2,
                        help="minimum occurrences before a candidate is reported (default 2)")
    parser.add_argument("--output", default=None,
                        help="write the markdown report here (default: stdout)")
    parser.add_argument(
        "--json-output",
        default=None,
        help="also write the machine-readable candidate report here",
    )
    args = parser.parse_args()

    paths: list[str] = []
    for pattern in args.events:
        matched = sorted(glob.glob(pattern))
        paths.extend(matched if matched else [pattern])

    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print(f"Event file(s) not found: {missing}", file=sys.stderr)
        return 2

    run_ids = set(args.run_id) if args.run_id else None
    events = list(iter_translation_events(paths, run_ids))
    if not events:
        print("No translation events matched.", file=sys.stderr)
        return 2

    try:
        candidate_data = build_candidate_data(events, min_count=args.min_count)
    except ValueError as exc:
        parser.error(str(exc))
    report = render_report(candidate_data)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Wrote {args.output} ({len(events)} events)")
    else:
        print(report)
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(candidate_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.json_output} ({len(events)} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
