from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "logs" / "live_translate.db"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def analyze_cache(db_path: Path = DEFAULT_DB_PATH, top_n: int = 10) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "db_path": str(db_path),
            "available": False,
            "reason": "database file does not exist",
        }

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "translations"):
            return {
                "db_path": str(db_path),
                "available": False,
                "reason": "translations table does not exist",
            }

        rows = conn.execute(
            """SELECT source_text, target_text, target_lang, engine, model,
                      hit_count, created_at, last_used_at, prompt_version
               FROM translations"""
        ).fetchall()

        return {
            "db_path": str(db_path),
            "available": True,
            "total_rows": len(rows),
            "total_hits": sum(int(row["hit_count"]) for row in rows),
            "by_engine_model_prompt": _by_engine_model_prompt(rows),
            "by_prompt_version": _by_prompt_version(rows),
            "daily_inserts": _daily_inserts(rows),
            "top_sources": _top_sources(rows, top_n),
            "output_length_outliers": _output_length_outliers(rows, top_n),
            "suspicious_untranslated": _suspicious_untranslated(rows, top_n),
        }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _by_engine_model_prompt(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["engine"], row["model"], row["prompt_version"])
        item = grouped.setdefault(
            key,
            {
                "engine": row["engine"],
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "rows": 0,
                "hits": 0,
            },
        )
        item["rows"] += 1
        item["hits"] += int(row["hit_count"])
    return sorted(grouped.values(), key=lambda item: (-item["rows"], -item["hits"], item["engine"]))


def _by_prompt_version(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        prompt_version = row["prompt_version"]
        item = grouped.setdefault(prompt_version, {"prompt_version": prompt_version, "rows": 0, "hits": 0})
        item["rows"] += 1
        item["hits"] += int(row["hit_count"])
    return sorted(grouped.values(), key=lambda item: (-item["rows"], item["prompt_version"]))


def _daily_inserts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[str, int] = {}
    for row in rows:
        day = str(row["created_at"])[:10] if row["created_at"] else "unknown"
        grouped[day] = grouped.get(day, 0) + 1
    return [{"date": day, "rows": rows} for day, rows in sorted(grouped.items())]


def _top_sources(rows: list[sqlite3.Row], top_n: int) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: int(row["hit_count"]), reverse=True)
    return [
        {
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "hit_count": int(row["hit_count"]),
            "engine": row["engine"],
            "model": row["model"],
            "prompt_version": row["prompt_version"],
        }
        for row in ordered[:top_n]
    ]


def _output_length_outliers(rows: list[sqlite3.Row], top_n: int) -> list[dict[str, Any]]:
    scored = []
    for row in rows:
        source_len = max(_non_space_len(row["source_text"]), 1)
        target_len = _non_space_len(row["target_text"])
        ratio = target_len / source_len
        if ratio >= 3.0 and target_len >= 30:
            scored.append((ratio, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "ratio": round(ratio, 2),
            "engine": row["engine"],
            "prompt_version": row["prompt_version"],
        }
        for ratio, row in scored[:top_n]
    ]


def _suspicious_untranslated(rows: list[sqlite3.Row], top_n: int) -> list[dict[str, Any]]:
    suspicious = []
    for row in rows:
        target = row["target_text"]
        source = row["source_text"]
        if target == source or _korean_ratio(target) > 0.5:
            suspicious.append(row)
    return [
        {
            "source_text": row["source_text"],
            "target_text": row["target_text"],
            "hit_count": int(row["hit_count"]),
            "engine": row["engine"],
            "prompt_version": row["prompt_version"],
        }
        for row in suspicious[:top_n]
    ]


def _non_space_len(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _korean_ratio(text: str) -> float:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return 0.0
    korean = sum(1 for char in chars if "\uac00" <= char <= "\ud7a3")
    return korean / len(chars)


def _print_report(report: dict[str, Any]) -> None:
    if not report.get("available"):
        print(f"Cache DB unavailable: {report['reason']} ({report['db_path']})")
        return

    print(f"Cache DB: {report['db_path']}")
    print(f"Rows: {report['total_rows']} | Total hits: {report['total_hits']}")
    print("\nBy engine/model/prompt:")
    for item in report["by_engine_model_prompt"]:
        print(
            f"- {item['engine']} / {item['model']} / {item['prompt_version']}: "
            f"{item['rows']} rows, {item['hits']} hits"
        )
    print("\nTop sources:")
    for item in report["top_sources"]:
        print(f"- hits={item['hit_count']} [{item['engine']}] {item['source_text'][:60]}")
    if report["output_length_outliers"]:
        print("\nOutput length outliers:")
        for item in report["output_length_outliers"]:
            print(f"- ratio={item['ratio']} [{item['engine']}] {item['source_text'][:60]}")
    if report["suspicious_untranslated"]:
        print("\nSuspicious untranslated rows:")
        for item in report["suspicious_untranslated"]:
            print(f"- hits={item['hit_count']} [{item['engine']}] {item['source_text'][:60]}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze SQLite translation cache behavior.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to live_translate.db.")
    parser.add_argument("--top", type=int, default=10, help="Number of top rows per report section.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = analyze_cache(args.db, args.top)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
