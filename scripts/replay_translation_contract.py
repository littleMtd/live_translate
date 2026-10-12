"""Reconstruct a frozen translation request from runtime contracts, offline.

This tool only reads JSONL and compares locally serialized bytes. It has no
provider transport or send mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.forensics_contract import (
    history_manifest, message_manifest, sha256_text, stable_identity,
)
from modules.translation_request import RouteRequest, TranslationRequest


def _messages(rows: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    result = tuple((str(row["role"]), str(row["content"])) for row in rows)
    if list(message_manifest(result)) != rows:
        raise ValueError("message manifest hash or index mismatch")
    return result


def _ordered_options(engine: str, recorded: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "deepseek": ("model", "temperature", "max_tokens", "stream", "thinking"),
        "groq": ("model", "temperature", "max_tokens", "reasoning_effort"),
    }.get(engine)
    if keys is None:
        raise ValueError(f"unknown engine: {engine}")
    expected = set(keys) - {"reasoning_effort"}
    if not expected.issubset(recorded):
        raise ValueError(f"missing required provider options for {engine}")
    return {key: recorded[key] for key in keys if key in recorded}


def _body(messages: tuple[tuple[str, str], ...], options: dict[str, Any],
          style: str) -> bytes:
    payload = {
        "model": options["model"],
        "messages": [{"role": role, "content": content} for role, content in messages],
        **{key: value for key, value in options.items() if key != "model"},
    }
    if style == "compact_utf8":
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if style == "python_default":
        return json.dumps(payload).encode()
    raise ValueError(f"unknown body JSON style: {style}")


def _route(event: dict[str, Any]) -> tuple[RouteRequest, list[dict[str, str]]]:
    engine = str(event["engine"])
    options = dict(event["provider_options"])
    messages = _messages(event["messages"])
    retry_messages = _messages(event.get("retry_messages") or [])
    style = str(event["route_body_json_style"])
    body = _body(messages, _ordered_options(engine, options), style)
    retry_body = None
    if retry_messages:
        retry_options = _ordered_options(engine, options)
        if engine == "groq":
            if "retry_max_tokens" not in options:
                raise ValueError("Groq retry options missing retry_max_tokens")
            retry_options["max_tokens"] = options["retry_max_tokens"]
        retry_body = _body(retry_messages, retry_options, style)
    diffs = []
    for label, expected, raw in (
        ("body_sha256", event.get("body_sha256"), body),
        ("retry_body_sha256", event.get("retry_body_sha256"), retry_body),
    ):
        actual = hashlib.sha256(raw).hexdigest() if raw is not None else ""
        if str(expected or "") != actual:
            diffs.append({"field": label, "expected": str(expected or ""), "actual": actual})
    route = RouteRequest(
        route_id=str(event["route_id"]), engine=engine,
        model=str(event["model"]), messages=messages,
        retry_messages=retry_messages, body=body, retry_body=retry_body,
        provider_options=tuple(
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            for key, value in options.items()
        ),
        timeout_seconds=event.get("timeout_seconds"),
    )
    return route, diffs


def reconstruct(events: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    selected = [row for row in events if row.get("event_type") == "translation_request_contract"
                and (row.get("translation_request_id") == request_id
                     or row.get("request_contract_id") == request_id)]
    if not selected:
        raise ValueError("request ID not found")
    versions = {int(row.get("request_contract_schema_version") or 1) for row in selected}
    if versions == {1}:
        if len(selected) != 1:
            raise ValueError("ambiguous v1 contract selection")
        row = selected[0]
        messages = _messages(row.get("messages") or [])
        return {
            "schema_version": 1,
            "request_contract_id": row.get("request_contract_id"),
            "messages": [list(pair) for pair in messages],
            "provider_options": "unavailable",
            "body_bytes": "unavailable",
            "differences": [],
        }
    if versions != {2}:
        raise ValueError("mixed or unsupported contract schema versions")
    count = int(selected[0]["route_count"])
    if len(selected) != count:
        raise ValueError(f"incomplete route set: expected {count}, found {len(selected)}")
    if {int(row["route_index"]) for row in selected} != set(range(count)):
        raise ValueError("duplicate or missing route index")
    selected.sort(key=lambda row: int(row["route_index"]))
    common_keys = (
        "translation_request_id", "route_count", "request_phase",
        "original_source", "provider_source", "incomplete", "history",
        "history_cohort", "history_context_enabled", "profile_cache_identity", "activity_cache_identity",
        "request_protection_identity", "canonical_obligations", "provisional_id",
        "artifact_hashes",
    )
    first = selected[0]
    for row in selected[1:]:
        if any(row.get(key) != first.get(key) for key in common_keys):
            raise ValueError("inconsistent all-route request fields")
    routes = []
    differences = []
    for row in selected:
        route, body_diffs = _route(row)
        routes.append(route)
        differences.extend({"route_id": route.route_id, **diff} for diff in body_diffs)
        checks = {
            "model": route.options_dict()["model"],
            "route_id": f"{route.engine}:{route.options_dict()['model']}",
            "provider_source_sha256": sha256_text(str(row["provider_source"])),
            "effective_system_prompt": route.messages[0][1],
            "effective_system_prompt_sha256": sha256_text(route.messages[0][1]),
            "messages_sha256": stable_identity({"messages": list(route.messages)}),
        }
        required = {
            "model", "route_id", "provider_source_sha256",
            "effective_system_prompt", "messages_sha256",
        }
        for field, actual in checks.items():
            if row.get(field) != actual and (field in row or field in required):
                differences.append({"route_id": route.route_id, "field": field,
                                    "expected": row.get(field), "actual": actual})
    history = tuple((str(row["source"]), str(row["target"]))
                    for row in first.get("history") or [])
    if list(history_manifest(history)) != (first.get("history") or []):
        raise ValueError("history manifest hash or index mismatch")
    cohort = first["history_cohort"]
    for row in selected:
        actual_cohort_id = f"{cohort[0]}:{cohort[1]}:{cohort[2]}"
        if "history_cohort_id" in row and row["history_cohort_id"] != actual_cohort_id:
            differences.append({"route_id": row["route_id"],
                                "field": "history_cohort_id",
                                "expected": row["history_cohort_id"],
                                "actual": actual_cohort_id})
        protection = row.get("request_protection")
        if isinstance(protection, dict) and protection.get("identity") != row["request_protection_identity"]:
            differences.append({"route_id": row["route_id"],
                                "field": "request_protection.identity",
                                "expected": protection.get("identity"),
                                "actual": row["request_protection_identity"]})
    request = TranslationRequest(
        original_source=str(first["original_source"]),
        provider_source=str(first["provider_source"]),
        incomplete=bool(first["incomplete"]),
        history=history,
        history_context_enabled=bool(first["history_context_enabled"]),
        history_cohort=(str(cohort[0]), str(cohort[1]), int(cohort[2])),
        profile_cache_identity=str(first["profile_cache_identity"]),
        activity_cache_identity=str(first["activity_cache_identity"]),
        request_protection_identity=str(first["request_protection_identity"]),
        canonical_obligations=tuple(
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in first.get("canonical_obligations") or []
        ),
        routes=tuple(routes), phase=str(first.get("request_phase") or "final"),
        provisional_id=str(first.get("provisional_id") or ""),
        artifact_hashes=tuple(sorted((first.get("artifact_hashes") or {}).items())),
    )
    if request.request_id != request_id:
        differences.append({"field": "translation_request_id", "expected": request_id,
                            "actual": request.request_id})
    for route, row in zip(routes, selected):
        actual = request.route_contract_id(route)
        if actual != row.get("request_contract_id"):
            differences.append({"route_id": route.route_id, "field": "request_contract_id",
                                "expected": row.get("request_contract_id"), "actual": actual})
    return {
        "schema_version": 2,
        "translation_request_id": request.request_id,
        "routes": [
            {"route_id": route.route_id,
             "messages": [list(pair) for pair in route.messages],
             "provider_options": route.options_dict(),
             "body_sha256": hashlib.sha256(route.body).hexdigest(),
             "retry_body_sha256": hashlib.sha256(route.retry_body).hexdigest()
             if route.retry_body is not None else ""}
            for route in routes
        ],
        "differences": differences,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path, help="runtime JSONL file")
    parser.add_argument("--request-id", required=True,
                        help="v2 translation_request_id or v1 request_contract_id")
    parser.add_argument("--run-id", default="", help="select one run in a daily JSONL")
    args = parser.parse_args()
    rows = []
    for line in args.events.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not args.run_id or row.get("run_id") == args.run_id:
                rows.append(row)
    report = reconstruct(rows, args.request_id)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["differences"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
