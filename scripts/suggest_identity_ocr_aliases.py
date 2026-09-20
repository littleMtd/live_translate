from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import glob
import json
from pathlib import Path
import re
import sys
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.identity_roi import normalize_identity
from modules.profile_context import load_registry_snapshot


DEFAULT_REGISTRY = PROJECT_ROOT / "data" / "streamer_profiles.json"
_SAFE_OBSERVATION_RE = re.compile(r"^[0-9a-z가-힣_!☆★♪♫:\-]{2,24}$", re.IGNORECASE)
_DECORATION_RE = re.compile(r"[_!☆★♪♫:\-0-9]+$")


@dataclass(frozen=True)
class ReviewedAlias:
    marker_id: str
    profile_id: str
    alias: str


def _hangul_units(text: str) -> tuple[str, ...]:
    units: list[str] = []
    for char in normalize_identity(text):
        code = ord(char)
        if 0xAC00 <= code <= 0xD7A3:
            offset = code - 0xAC00
            units.extend((
                f"L{offset // 588}",
                f"V{(offset % 588) // 28}",
                f"T{offset % 28}",
            ))
        else:
            units.append(char)
    return tuple(units)


def _edit_distance(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, left_item in enumerate(left, 1):
        current = [row]
        for column, right_item in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_item != right_item),
            ))
        previous = current
    return previous[-1]


def _distance(left: str, right: str) -> float:
    left_units = _hangul_units(left)
    right_units = _hangul_units(right)
    return _edit_distance(left_units, right_units) / max(1, len(left_units), len(right_units))


def _base_length(value: str) -> int:
    return len(_DECORATION_RE.sub("", normalize_identity(value)))


def reviewed_aliases(registry_path: Path = DEFAULT_REGISTRY) -> tuple[ReviewedAlias, ...]:
    registry = load_registry_snapshot(registry_path, version=1)
    rows: list[ReviewedAlias] = []
    for marker in registry.identity_markers:
        if marker.kind != "member_name":
            continue
        for alias in (*marker.visible_names, *marker.ocr_aliases):
            rows.append(ReviewedAlias(
                marker_id=marker.marker_id,
                profile_id=marker.profile_id,
                alias=normalize_identity(alias),
            ))
    return tuple(dict.fromkeys(rows))


def collect_observations(paths: Iterable[Path]) -> tuple[Counter[str], dict[str, set[str]]]:
    counts: Counter[str] = Counter()
    runs: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if event.get("event_type") != "profile_resolution":
                    continue
                if event.get("identity_authority") != "calibrated_channel_identity_roi":
                    continue
                if event.get("identity_read_attempted") is not True:
                    continue
                if event.get("reason") != "identity_blank_or_not_reviewed":
                    continue
                observed = normalize_identity(event.get("normalized_observed_identity"))
                if not observed or not _SAFE_OBSERVATION_RE.fullmatch(observed):
                    continue
                counts[observed] += 1
                runs[observed].add(str(event.get("run_id") or path.name))
    return counts, runs


def analyze(
    event_paths: Iterable[Path],
    *,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, object]:
    event_paths = tuple(dict.fromkeys(Path(path).resolve() for path in event_paths))
    aliases = reviewed_aliases(registry_path)
    exact = defaultdict(set)
    for row in aliases:
        exact[row.alias].add(row.marker_id)
    counts, runs = collect_observations(event_paths)
    candidates: list[dict[str, object]] = []
    for observed, count in counts.items():
        if observed in exact:
            continue
        per_marker: dict[str, tuple[float, ReviewedAlias]] = {}
        for row in aliases:
            if abs(_base_length(observed) - _base_length(row.alias)) > 1:
                continue
            distance = _distance(observed, row.alias)
            current = per_marker.get(row.marker_id)
            if current is None or distance < current[0]:
                per_marker[row.marker_id] = (distance, row)
        ranked = sorted(per_marker.values(), key=lambda item: (item[0], item[1].marker_id))
        if not ranked or ranked[0][0] > 0.5:
            continue
        best_distance, best = ranked[0]
        second_distance = ranked[1][0] if len(ranked) > 1 else 1.0
        margin = second_distance - best_distance
        unique_owner = margin >= 0.10
        run_count = len(runs[observed])
        status = (
            "review"
            if count >= 2 and unique_owner and best_distance <= 0.40
            else "insufficient"
        )
        candidates.append({
            "observed": observed,
            "count": count,
            "run_count": run_count,
            "candidate_marker_id": best.marker_id,
            "candidate_profile_id": best.profile_id,
            "nearest_reviewed_alias": best.alias,
            "distance": round(best_distance, 4),
            "owner_margin": round(margin, 4),
            "unique_owner": unique_owner,
            "status": status,
            "production_action": "manual_review_only",
        })
    candidates.sort(key=lambda row: (
        row["status"] != "review",
        -int(row["count"]),
        float(row["distance"]),
        str(row["observed"]),
    ))
    return {
        "schema_version": 1,
        "policy": {
            "runtime_activation": "none",
            "registry_mutation": "none",
            "minimum_review_count": 2,
            "maximum_review_distance": 0.40,
            "minimum_owner_margin": 0.10,
        },
        "event_files": [str(path) for path in event_paths],
        "candidate_count": len(candidates),
        "review_count": sum(row["status"] == "review" for row in candidates),
        "candidates": candidates,
    }


def render_markdown(report: dict[str, object]) -> str:
    rows = report["candidates"]
    lines = [
        "# Identity OCR alias candidates",
        "",
        "This report is advisory. It does not activate aliases or modify the registry.",
        "",
        "| Status | Observed | Count | Runs | Nearest reviewed alias | Marker | Distance | Margin |",
        "|---|---:|---:|---:|---|---|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['status']} | `{row['observed']}` | {row['count']} | "
            f"{row['run_count']} | `{row['nearest_reviewed_alias']}` | "
            f"`{row['candidate_marker_id']}` | {row['distance']:.4f} | "
            f"{row['owner_margin']:.4f} |"
        )
    lines.extend(("", f"Review candidates: {report['review_count']} / {report['candidate_count']}"))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suggest reviewed identity-OCR aliases from persisted runtime observations."
    )
    parser.add_argument("events", nargs="+")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args()
    event_paths: list[Path] = []
    for raw in args.events:
        matches = sorted(glob.glob(raw)) if any(char in raw for char in "*?[") else [raw]
        event_paths.extend(Path(match) for match in matches)
    if not event_paths:
        parser.error("no runtime event files matched")
    report = analyze(event_paths, registry_path=args.registry)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
