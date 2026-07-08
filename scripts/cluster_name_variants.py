"""Cluster STT spelling variants among Hangul-leak candidates (offline, read-only).

Item 1 of DETERMINISTIC_FIXES_PROPOSAL_20260624.md. This is the net-new layer on
top of scripts/suggest_corrections.py: that script detects per-profile Hangul leaks
in zh-TW output via a flag-free regex + allowlist (so it works on schema_version 1
and 2 alike); this script groups those leaks into spelling-variant clusters
(채나/챈나/채냐 ... -> one name) so a human can assign one canonical per cluster.

It does not mutate data/. It writes a candidate table + the human-review artifacts
the proposal's gate requires (similarity threshold used, full stop-list applied,
cross-profile conflicts, boundary pairs near the threshold) to scratch/analysis/.

Leak detection is decoupled from the target_has_hangul quality flag (the proposal
flagged flag-based measurement as circular and v1-blind). Coverage of the v2-only
quality flags is reported separately for context, but the leak signal here does not
depend on it.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.suggest_corrections import (  # noqa: E402
    build_hangul_allowlist,
    find_hangul_leaks,
    iter_translation_events,
)

# Illustrative, NOT exhaustive. Any token of the same class (honorific, particle,
# filler, vocalization) is excluded the same way; these are the multi-char ones seen
# in the 0613-0624 scans. Single-char particles never reach here (the leak regex
# requires length >= 2).
DEFAULT_STOP_LIST = frozenset({
    "으흥", "으흐흐", "크크", "크크크", "흐흐", "하하", "하하하",
    "님", "씨", "요", "예", "응",
})
_HANGUL_RUN_RE = re.compile(r"[가-힣]{2,}")
_MAX_REVIEW_EXAMPLES = 3

# Jamo composition constants for modern Hangul syllables (U+AC00..U+D7A3).
_HANGUL_BASE = 0xAC00
_HANGUL_LAST = 0xD7A3
_LEAD = ["ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ", "ㅅ", "ㅆ", "ㅇ",
         "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
_VOWEL = ["ㅏ", "ㅐ", "ㅑ", "ㅒ", "ㅓ", "ㅔ", "ㅕ", "ㅖ", "ㅗ", "ㅘ", "ㅙ", "ㅚ",
          "ㅛ", "ㅜ", "ㅝ", "ㅞ", "ㅟ", "ㅠ", "ㅡ", "ㅢ", "ㅣ"]
_TAIL = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ",
         "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ",
         "ㅌ", "ㅍ", "ㅎ"]


def to_jamo(text: str) -> list[str]:
    """Decompose a Hangul string into a flat jamo list; non-syllables pass through."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _HANGUL_BASE <= code <= _HANGUL_LAST:
            idx = code - _HANGUL_BASE
            lead = idx // (21 * 28)
            vowel = (idx % (21 * 28)) // 28
            tail = idx % 28
            out.append(_LEAD[lead])
            out.append(_VOWEL[vowel])
            if _TAIL[tail]:
                out.append(_TAIL[tail])
        else:
            out.append(ch)
    return out


def edit_distance(a: list[str], b: list[str]) -> int:
    """Levenshtein over jamo sequences."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def jamo_distance(token_a: str, token_b: str) -> tuple[int, float]:
    """Return (absolute jamo edit distance, normalized by the longer jamo length)."""
    ja, jb = to_jamo(token_a), to_jamo(token_b)
    dist = edit_distance(ja, jb)
    denom = max(len(ja), len(jb)) or 1
    return dist, dist / denom


def cluster_tokens(
    tokens: list[str],
    counts: dict[str, int],
    *,
    norm_threshold: float,
) -> tuple[list[list[str]], list[dict]]:
    """Union-find cluster tokens whose normalized jamo distance <= threshold.

    Returns (clusters, boundary_pairs). Clusters are lists of tokens ordered by
    descending count (representative first). boundary_pairs records token pairs whose
    normalized distance lands just inside or just outside the threshold, for human
    review of the threshold choice.
    """
    parent = {t: t for t in tokens}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    boundary: list[dict] = []
    band = 0.10  # report pairs within +/- band of the threshold
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            a, b = tokens[i], tokens[j]
            dist, norm = jamo_distance(a, b)
            if norm <= norm_threshold:
                union(a, b)
            if abs(norm - norm_threshold) <= band:
                boundary.append({
                    "a": a, "b": b, "jamo_distance": dist, "normalized": round(norm, 3),
                    "relation": "just_inside" if norm <= norm_threshold else "just_outside",
                })
    groups: dict[str, list[str]] = {}
    for t in tokens:
        groups.setdefault(find(t), []).append(t)
    clusters = [
        sorted(members, key=lambda t: (-counts[t], t))
        for members in groups.values()
    ]
    clusters.sort(key=lambda members: -sum(counts[m] for m in members))
    boundary.sort(key=lambda p: abs(p["normalized"] - norm_threshold))
    return clusters, boundary


def build_table(
    *,
    events: list[dict],
    min_count: int,
    norm_threshold: float,
    stop_list: frozenset[str],
) -> dict:
    allowlist, suffixes = build_hangul_allowlist()
    # The adopted proposal requires every leak count used for decisions to be scoped
    # to the v2-covered population.  The detector itself is flag-free, but running it
    # over v1 would still turn uncovered rows into decision evidence.
    v2_events = [event for event in events if event.get("schema_version") == 2]
    leaks = [
        candidate
        for candidate in find_hangul_leaks(v2_events, allowlist, suffixes)
        if candidate.count >= min_count
    ]

    # Coverage is reported over all loaded translation events; v1 stays explicitly
    # uncovered and never contributes to the candidate counts below.
    total = len(events)
    v2 = len(v2_events)
    days = {str(e.get("created_at", ""))[:10] for e in events if e.get("created_at")}
    days_v2 = {str(e.get("created_at", ""))[:10] for e in events
               if e.get("schema_version") == 2 and e.get("created_at")}

    stop_hits = []
    leaks_kept = []
    for c in leaks:
        if c.token in stop_list:
            stop_hits.append({"token": c.token, "count": c.count, "profiles": dict(c.profiles)})
        else:
            leaks_kept.append(c)

    count_by_token = {c.token: c.count for c in leaks_kept}
    examples_by_profile_token: dict[tuple[str, str], list[dict]] = {}
    for event in v2_events:
        if event.get("status") != "success":
            continue
        profile = str(event.get("profile_id") or "(none)")
        target = str(event.get("target_text") or "")
        for token in set(_HANGUL_RUN_RE.findall(target)):
            if token not in count_by_token:
                continue
            key = (profile, token)
            examples = examples_by_profile_token.setdefault(key, [])
            if len(examples) >= _MAX_REVIEW_EXAMPLES:
                continue
            examples.append({
                "run_id": event.get("run_id"),
                "sequence_id": event.get("sequence_id"),
                "source_text": str(event.get("source_text") or ""),
                "target_text": target,
            })
    cross_profile = [
        {"token": c.token, "profiles": dict(c.profiles)}
        for c in leaks_kept if len([p for p in c.profiles if p]) > 1
    ]

    # Cluster within each profile (per-profile is the proposal's unit; a token that
    # leaks in several profiles is clustered in each independently).
    by_profile: dict[str, dict[str, int]] = {}
    for c in leaks_kept:
        for profile, n in c.profiles.items():
            by_profile.setdefault(profile or "(none)", {})[c.token] = (
                by_profile.setdefault(profile or "(none)", {}).get(c.token, 0) + n
            )

    profiles_out: dict[str, dict] = {}
    boundary_all: list[dict] = []
    for profile, counts in sorted(by_profile.items(), key=lambda kv: -sum(kv[1].values())):
        tokens = list(counts)
        clusters, boundary = cluster_tokens(tokens, counts, norm_threshold=norm_threshold)
        for p in boundary:
            p["profile"] = profile
        boundary_all.extend(boundary)
        cluster_rows = []
        singletons = []
        for members in clusters:
            total_count = sum(counts[m] for m in members)
            if len(members) == 1:
                singletons.append({"token": members[0], "count": counts[members[0]]})
                continue
            cluster_rows.append({
                "representative": members[0],
                "members": [
                    {
                        "token": member,
                        "count": counts[member],
                        "examples": examples_by_profile_token.get((profile, member), []),
                    }
                    for member in members
                ],
                "total_count": total_count,
                "suggested_wrong_forms": members,
                "canonical": None,  # human fills this
            })
        for row in singletons:
            row["examples"] = examples_by_profile_token.get((profile, row["token"]), [])
        profiles_out[profile] = {
            "leak_instances": sum(counts.values()),
            "distinct_tokens": len(tokens),
            "variant_clusters": cluster_rows,
            "singletons": singletons,
        }

    return {
        "name_normalization_schema": 1,
        "regenerate_command": (
            "live-subtitle-env\\Scripts\\python.exe scripts\\cluster_name_variants.py "
            "--events \"logs/runtime_events_202605*.jsonl\" "
            "\"logs/runtime_events_2026060*.jsonl\" "
            "\"logs/runtime_events_2026061*.jsonl\" "
            "\"logs/runtime_events_2026062[0-4].jsonl\" "
            f"--min-count {min_count} --threshold {norm_threshold}"
        ),
        "leak_detection": {
            "method": "flag-free Hangul-run regex vs data/ allowlist "
                      "(reused from scripts/suggest_corrections.py find_hangul_leaks)",
            "status_filter": "success only",
            "min_count": min_count,
            "decoupled_from_quality_flag": True,
            "decision_population": "schema_version == 2 success translations only",
        },
        "schema_version_coverage": {
            "translation_events_total": total,
            "schema_v2": v2,
            "schema_v1_or_missing": total - v2,
            "v2_ratio": round(v2 / total, 4) if total else 0.0,
            "days_total": len(days),
            "days_with_v2": len(days_v2),
            "note": "Candidate detection is flag-free but deliberately runs only on v2. "
                    "v1 spans are uncovered, not clean, and do not contribute to any "
                    "candidate count.",
        },
        "clustering": {
            "normalized_jamo_edit_distance_threshold": norm_threshold,
            "unit": "per-profile",
            "boundary_band": 0.10,
        },
        "stop_list": {
            "applied": sorted(stop_list),
            "note": "Illustrative, not exhaustive. Honorifics/particles/fillers/"
                    "vocalizations of the same class are excluded the same way.",
            "hits": stop_hits,
        },
        "cross_profile_tokens": cross_profile,
        "boundary_pairs": boundary_all,
        "profiles": profiles_out,
        "measurement": {
            "targeted_leak_instances": sum(
                profile["leak_instances"] for profile in profiles_out.values()
            ),
            "redetected_after_rewrite": None,
            "redetection_status": "blocked_on_human_canonical_approval",
            "stop_list_hit_instances": sum(hit["count"] for hit in stop_hits),
            "boundary_pair_human_fp_estimate": None,
            "boundary_review_status": "pending_human_review",
        },
        "human_review_required": [
            "Assign one canonical per variant_cluster (canonical is null).",
            "Confirm the clustering threshold from the boundary_pairs.",
            "Resolve cross_profile_tokens per profile; do not auto-merge across profiles.",
            "Approve before any entry reaches data/translation_corrections.json or "
            "data/streamer_profiles.json.",
        ],
    }


def render_review_markdown(table: dict) -> str:
    """Render the machine-readable table as a bounded human sign-off worksheet."""
    coverage = table["schema_version_coverage"]
    measurement = table["measurement"]
    lines = [
        "# Name normalization candidate review",
        "",
        "> No data file is changed by this worksheet. Fill canonical/decision fields, then approve explicitly.",
        "",
        "## Scope and gate",
        "",
        f"- Decision population: schema v2 only ({coverage['schema_v2']}/{coverage['translation_events_total']} loaded events).",
        f"- v1/missing rows: {coverage['schema_v1_or_missing']} uncovered; excluded from every candidate count.",
        f"- Targeted leak instances before any rewrite: {measurement['targeted_leak_instances']}.",
        "- Post-rewrite re-detection: blocked until a human approves canonical renderings.",
        f"- Clustering threshold: {table['clustering']['normalized_jamo_edit_distance_threshold']} normalized jamo edit distance.",
        f"- Applied stop-list: {', '.join(table['stop_list']['applied']) or '(empty)'}.",
        "",
        "## Candidate table",
        "",
    ]
    for profile, data in table["profiles"].items():
        lines.extend([
            f"### `{profile}`",
            "",
            f"Leak instances: {data['leak_instances']}; distinct tokens: {data['distinct_tokens']}.",
            "",
        ])
        for index, cluster in enumerate(data["variant_clusters"], start=1):
            members = ", ".join(
                f"`{member['token']}` ×{member['count']}" for member in cluster["members"]
            )
            lines.extend([
                f"#### Cluster {index}: {members}",
                "",
                "- [ ] Approve as one name cluster",
                "- [ ] Reject / split cluster",
                "- Canonical Korean source: `________________`",
                "- Canonical zh-TW rendering: `________________`",
                "",
            ])
            for member in cluster["members"]:
                for example in member["examples"]:
                    lines.extend([
                        f"  - `{member['token']}` — source: {example['source_text']}",
                        f"    target: {example['target_text']}",
                    ])
            lines.append("")
        if data["singletons"]:
            lines.extend(["#### Recurring singletons", ""])
            for singleton in data["singletons"]:
                lines.append(f"- [ ] `{singleton['token']}` ×{singleton['count']}: name / intentional Hangul / noise")
                for example in singleton["examples"]:
                    lines.extend([
                        f"  - source: {example['source_text']}",
                        f"    target: {example['target_text']}",
                    ])
            lines.append("")

    lines.extend(["## Cross-profile tokens", ""])
    if table["cross_profile_tokens"]:
        for row in table["cross_profile_tokens"]:
            profiles = ", ".join(f"{profile}×{count}" for profile, count in row["profiles"].items())
            lines.append(f"- [ ] `{row['token']}` — {profiles}; resolve independently per profile")
    else:
        lines.append("None.")

    lines.extend(["", "## Threshold boundary pairs", ""])
    if table["boundary_pairs"]:
        for pair in table["boundary_pairs"]:
            lines.append(
                f"- [ ] `{pair['a']}` ↔ `{pair['b']}` ({pair['profile']}, "
                f"distance {pair['normalized']}, {pair['relation']})"
            )
    else:
        lines.append("None in the configured boundary band.")

    lines.extend(["", "## Stop-list hits", ""])
    if table["stop_list"]["hits"]:
        for hit in table["stop_list"]["hits"]:
            lines.append(f"- `{hit['token']}` ×{hit['count']} — excluded")
    else:
        lines.append("None at the configured minimum count.")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cluster STT spelling variants among Hangul leaks.")
    parser.add_argument("--events", nargs="+", required=True, help="runtime-event JSONL file(s) or glob(s)")
    parser.add_argument("--min-count", type=int, default=2)
    parser.add_argument("--threshold", type=float, default=0.34,
                        help="normalized jamo edit-distance threshold for variant clustering")
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / "scratch" / "analysis" / "name_normalization_20260624.json")
    parser.add_argument(
        "--review-output",
        type=Path,
        default=PROJECT_ROOT / "scratch" / "analysis" / "name_normalization_review_20260624.md",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths: list[str] = []
    for pattern in args.events:
        paths.extend(sorted(glob.glob(pattern)))
    if not paths:
        print("no event files matched", file=sys.stderr)
        return 1
    events = list(iter_translation_events(paths))
    table = build_table(
        events=events,
        min_count=args.min_count,
        norm_threshold=args.threshold,
        stop_list=DEFAULT_STOP_LIST,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.review_output.parent.mkdir(parents=True, exist_ok=True)
    args.review_output.write_text(render_review_markdown(table), encoding="utf-8")
    n_clusters = sum(len(p["variant_clusters"]) for p in table["profiles"].values())
    print(f"Wrote {args.output} and {args.review_output} | profiles={len(table['profiles'])} "
          f"multi-token clusters={n_clusters} v2_ratio={table['schema_version_coverage']['v2_ratio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
