"""Offline correction-suggestion generator.

Scans translation runtime events and surfaces *candidate* glossary /
correction entries for human review — the validated replacement for the
removed PromptEvolver (see CODE_REVIEW_20260611.md P4): the machine mines,
a human decides, approved entries go into data/translation_corrections.json
or data/translation_profiles.json.

Detectors:
1. hangul_leaks — Hangul runs in zh-TW output that are not part of the
   intentional keep-list (profile glossaries, correction canonicals, slang).
   These are either STT mishearings of known names (-> source_norm) or
   missing glossary entries (-> profile / name_rendering_rules).
2. inconsistent_translations — the same source sentence produced different
   target sentences across the run(s). High-frequency offenders are
   glossary candidates.
3. quality summary — warn/bad counts and examples, for eyeballing.

Usage:
    python scripts/suggest_corrections.py --events logs/runtime_events_20260611.jsonl
    python scripts/suggest_corrections.py --events "logs/runtime_events_2026061*.jsonl" \
        --run-id 20260611T035026Z-30856 --min-count 2 --output logs/suggestions.md
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_DATA_DIR = PROJECT_ROOT / "data"
_HANGUL_RUN_RE = re.compile(r"[가-힣]{2,}")
_MAX_EXAMPLES = 3


# ---------------------------------------------------------------------------
# Event loading
# ---------------------------------------------------------------------------

def iter_translation_events(paths: list[str], run_ids: set[str] | None = None):
    """Yield translation events from runtime-event JSONL files.

    Tolerates BOMs, blank lines and corrupt lines (live logs get truncated)."""
    for path in paths:
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

@dataclass
class LeakCandidate:
    token: str
    count: int = 0
    profiles: Counter = field(default_factory=Counter)
    examples: list[tuple[str, str]] = field(default_factory=list)  # (source, target)


def find_hangul_leaks(
    events: list[dict],
    allowlist: frozenset[str],
    suffixes: frozenset[str],
) -> list[LeakCandidate]:
    candidates: dict[str, LeakCandidate] = {}
    for event in events:
        if event.get("status") != "success":
            continue
        target = event.get("target_text") or ""
        source = event.get("source_text") or ""
        for token in set(_hangul_runs(target)):
            if _is_allowed(token, allowlist, suffixes):
                continue
            entry = candidates.setdefault(token, LeakCandidate(token))
            entry.count += 1
            entry.profiles[event.get("profile_id") or ""] += 1
            if len(entry.examples) < _MAX_EXAMPLES:
                entry.examples.append((source, target))
    return sorted(candidates.values(), key=lambda c: -c.count)


@dataclass
class InconsistencyCandidate:
    source: str
    variants: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.variants.values())


def find_inconsistent_translations(events: list[dict]) -> list[InconsistencyCandidate]:
    by_source: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        if event.get("status") != "success":
            continue
        source = " ".join((event.get("source_text") or "").split())
        target = (event.get("target_text") or "").strip()
        if source and target:
            by_source[source][target] += 1
    results = [
        InconsistencyCandidate(source, variants)
        for source, variants in by_source.items()
        if len(variants) >= 2
    ]
    return sorted(results, key=lambda c: -c.total)


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

def _leak_rule_skeleton(candidate: LeakCandidate) -> str:
    profile = candidate.profiles.most_common(1)[0][0] if candidate.profiles else ""
    scope = profile or "__shared__"
    return (
        f'source_norm (若為 STT 誤聽): "{candidate.token}": "<正確韓文>"  (profiles.{scope})\n'
        f'  name_rendering (若為缺詞條): {{"scope": "{scope}", '
        f'"source_aliases": ["{candidate.token}"], "wrong_forms": ["{candidate.token}"], '
        f'"canonical": "<正確寫法>"}}'
    )


def build_report(
    events: list[dict],
    *,
    min_count: int = 1,
    data_dir: Path = _DATA_DIR,
) -> str:
    allowlist, suffixes = build_hangul_allowlist(data_dir)
    leaks = [c for c in find_hangul_leaks(events, allowlist, suffixes) if c.count >= min_count]
    inconsistencies = [
        c for c in find_inconsistent_translations(events) if c.total >= max(min_count, 2)
    ]
    quality = summarize_quality(events)

    lines: list[str] = []
    lines.append("# Correction Suggestions")
    lines.append("")
    lines.append(f"Translation events scanned: {len(events)}")
    lines.append(f"Allowlist terms: {len(allowlist)} (from profiles/corrections/slang)")
    lines.append("")

    lines.append(f"## 1. Hangul leaks in zh-TW output ({len(leaks)})")
    lines.append("")
    if leaks:
        lines.append("輸出中出現、但不在「有意保留」清單的韓文片段。")
        lines.append("人工判斷:STT 誤聽 → source_norm;缺詞條 → profile/name_rendering。")
        lines.append("")
        for c in leaks:
            profile_note = ", ".join(f"{p or '(no profile)'}×{n}" for p, n in c.profiles.most_common())
            lines.append(f"### `{c.token}` — {c.count} 次 ({profile_note})")
            for source, target in c.examples:
                lines.append(f"- KO: {source[:80]}")
                lines.append(f"  ZH: {target[:80]}")
            lines.append("```")
            lines.append(_leak_rule_skeleton(c))
            lines.append("```")
            lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    lines.append(f"## 2. Inconsistent translations ({len(inconsistencies)})")
    lines.append("")
    if inconsistencies:
        lines.append("同一源句出現多種譯法(高頻者值得收進詞彙表固定下來)。")
        lines.append("")
        for c in inconsistencies[:20]:
            lines.append(f"### {c.source[:80]} — {c.total} 次 / {len(c.variants)} 種")
            for target, n in c.variants.most_common():
                lines.append(f"- ×{n}: {target[:80]}")
            lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    lines.append("## 3. Quality summary")
    lines.append("")
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
                        help="minimum occurrences before a leak is reported (default 2)")
    parser.add_argument("--output", default=None,
                        help="write the markdown report here (default: stdout)")
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

    report = build_report(events, min_count=args.min_count)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Wrote {args.output} ({len(events)} events)")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
