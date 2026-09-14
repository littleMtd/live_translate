"""Evidence-only causal analysis and integrity validation for runtime bundles."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from modules.forensics_contract import sha256_text, stable_identity
from utils.chatgpt_bundle import bundle_event_paths


ANALYSIS_SCHEMA_VERSION = 1


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _events(bundle: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in bundle_event_paths(bundle):
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                result.append({
                    "event_type": "__invalid_json__",
                    "source_file": path.name,
                    "source_line": line_number,
                })
                continue
            if isinstance(event, dict):
                event = dict(event)
                event["_bundle_ordinal"] = len(result) + 1
                result.append(event)
    return result


def _ids(event: dict[str, Any], singular: str, plural: str) -> list[str]:
    values: list[str] = []
    if event.get(singular):
        values.append(str(event[singular]))
    raw = event.get(plural)
    if isinstance(raw, list):
        values.extend(str(item) for item in raw if item)
    return list(dict.fromkeys(values))


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    event_ordinal: int | None = None,
    reference: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "event_ordinal": event_ordinal,
        "reference": reference,
    }


def _verify_manifest(bundle: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    path = bundle / "manifest.json"
    if not path.is_file():
        issues.append(_issue("missing_manifest", "manifest.json is absent"))
        return {}
    try:
        manifest = _json(path)
    except (json.JSONDecodeError, ValueError):
        issues.append(_issue("invalid_manifest", "manifest.json is not valid JSON"))
        return {}
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        issues.append(_issue("missing_integrity_index", "manifest has no integrity index"))
        return manifest
    root = bundle.resolve()
    referenced_files = {
        str(name)
        for key in ("runtime_event_files", "derived_files")
        for name in (manifest.get(key) or [])
        if name
    }
    for name in sorted(referenced_files):
        candidate = (bundle / name).resolve()
        if (candidate.parent != root and root not in candidate.parents) or not candidate.is_file():
            issues.append(_issue("missing_referenced_bundle_file", "manifest references an absent or unsafe file", reference=name))
    for name, expected in integrity.items():
        candidate = (bundle / str(name)).resolve()
        if candidate.parent != root and root not in candidate.parents:
            issues.append(_issue("unsafe_artifact_path", "integrity path escapes bundle", reference=str(name)))
            continue
        if not candidate.is_file():
            issues.append(_issue("missing_bundle_artifact", "indexed bundle artifact is absent", reference=str(name)))
            continue
        actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        expected_hash = str(expected.get("sha256") or "") if isinstance(expected, dict) else ""
        if expected_hash != actual_hash:
            issues.append(_issue("bundle_artifact_hash_mismatch", "bundle artifact hash differs from manifest", reference=str(name)))
        expected_size = expected.get("size_bytes") if isinstance(expected, dict) else None
        if isinstance(expected_size, int) and candidate.stat().st_size != expected_size:
            issues.append(_issue("bundle_artifact_size_mismatch", "bundle artifact size differs from manifest", reference=str(name)))
    return manifest


def _verify_translation_contract(contract: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    ordinal = contract.get("_bundle_ordinal")
    contract_id = str(contract.get("request_contract_id") or "")
    source = str(contract.get("provider_source") or "")
    if str(contract.get("provider_source_sha256") or "") != sha256_text(source):
        issues.append(_issue("provider_source_hash_mismatch", "provider source hash is invalid", event_ordinal=ordinal, reference=contract_id))
    prompt = str(contract.get("effective_system_prompt") or "")
    recorded_prompt_hash = str(contract.get("effective_system_prompt_sha256") or "")
    if recorded_prompt_hash and recorded_prompt_hash != sha256_text(prompt):
        issues.append(_issue("system_prompt_hash_mismatch", "effective system prompt hash is invalid", event_ordinal=ordinal, reference=contract_id))
    messages = contract.get("messages")
    if not isinstance(messages, list):
        issues.append(_issue("missing_message_manifest", "translation contract has no message manifest", event_ordinal=ordinal, reference=contract_id))
        return
    tuples: list[tuple[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            issues.append(_issue("invalid_message_manifest", "message row is not an object", event_ordinal=ordinal, reference=contract_id))
            continue
        content = str(message.get("content") or "")
        if message.get("index") != index:
            issues.append(_issue("message_index_mismatch", "message indexes are not contiguous", event_ordinal=ordinal, reference=contract_id))
        if str(message.get("content_sha256") or "") != sha256_text(content):
            issues.append(_issue("message_hash_mismatch", "message content hash is invalid", event_ordinal=ordinal, reference=contract_id))
        if message.get("char_count") != len(content):
            issues.append(_issue("message_length_mismatch", "message length is invalid", event_ordinal=ordinal, reference=contract_id))
        tuples.append((str(message.get("role") or ""), content))
    expected = str(contract.get("messages_sha256") or "")
    if expected and expected != stable_identity({"messages": tuples}):
        issues.append(_issue("messages_hash_mismatch", "combined messages hash is invalid", event_ordinal=ordinal, reference=contract_id))
    history = contract.get("history", [])
    if isinstance(history, list):
        for row in history:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source") or "")
            target = str(row.get("target") or "")
            if str(row.get("source_sha256") or "") != sha256_text(source) or str(row.get("target_sha256") or "") != sha256_text(target):
                issues.append(_issue("history_hash_mismatch", "history item hash is invalid", event_ordinal=ordinal, reference=contract_id))
            pair_hash = str(row.get("pair_sha256") or "")
            if pair_hash and pair_hash != stable_identity({"source": source, "target": target}):
                issues.append(_issue("history_pair_hash_mismatch", "history pair hash is invalid", event_ordinal=ordinal, reference=contract_id))


def _verify_stt_contract(contract: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    ordinal = contract.get("_bundle_ordinal")
    contract_id = str(contract.get("stt_request_contract_id") or "")
    parameters = contract.get("request_parameters")
    if not isinstance(parameters, dict):
        issues.append(_issue("missing_stt_parameters", "STT contract has no request parameters", event_ordinal=ordinal, reference=contract_id))
        return
    if "prompt" in parameters:
        expected = str(contract.get("prompt_sha256") or "")
        if expected != sha256_text(str(parameters.get("prompt") or "")):
            issues.append(_issue("stt_prompt_hash_mismatch", "STT prompt hash is invalid", event_ordinal=ordinal, reference=contract_id))
    keyterms = parameters.get("keyterms", [])
    expected_keyterms = str(contract.get("keyterms_sha256") or "")
    if expected_keyterms and expected_keyterms != stable_identity({"keyterms": keyterms}):
        issues.append(_issue("stt_keyterms_hash_mismatch", "STT keyterms hash is invalid", event_ordinal=ordinal, reference=contract_id))


def analyze_forensics_bundle(bundle: Path) -> dict[str, Any]:
    bundle = Path(bundle)
    issues: list[dict[str, Any]] = []
    manifest = _verify_manifest(bundle, issues)
    events = _events(bundle)
    if any(event.get("event_type") == "__invalid_json__" for event in events):
        issues.append(_issue("invalid_runtime_json", "one or more runtime JSONL rows are invalid"))

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_type[str(event.get("event_type") or "unknown")].append(event)

    translation_contracts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stt_contracts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contract in by_type["translation_request_contract"]:
        contract_id = str(contract.get("request_contract_id") or "")
        if not contract_id:
            issues.append(_issue("missing_contract_id", "translation contract has no ID", event_ordinal=contract.get("_bundle_ordinal")))
        else:
            translation_contracts[contract_id].append(contract)
        _verify_translation_contract(contract, issues)
    for contract in by_type["stt_request_contract"]:
        contract_id = str(contract.get("stt_request_contract_id") or "")
        if not contract_id:
            issues.append(_issue("missing_contract_id", "STT contract has no ID", event_ordinal=contract.get("_bundle_ordinal")))
        else:
            stt_contracts[contract_id].append(contract)
        _verify_stt_contract(contract, issues)
        if contract.get("request_audio_wav_sha256"):
            issues.append(_issue(
                "provider_request_audio_not_bundled",
                "provider-bound normalized WAV hash is recorded but those exact bytes are absent",
                severity="unresolved",
                event_ordinal=contract.get("_bundle_ordinal"),
                reference=contract_id,
            ))

    for contract_id, rows in (*translation_contracts.items(), *stt_contracts.items()):
        if len(rows) > 1:
            normalized = [{key: value for key, value in row.items() if key not in {"created_at", "_bundle_ordinal"}} for row in rows]
            code = "conflicting_contract_id" if any(row != normalized[0] for row in normalized[1:]) else "duplicate_contract_id"
            issues.append(_issue(code, "contract ID resolves to multiple rows", reference=contract_id))

    contract_index_path = bundle / "request_contracts.json"
    if not contract_index_path.is_file():
        issues.append(_issue("missing_contract_index", "request_contracts.json is absent"))
    else:
        try:
            indexed = _json(contract_index_path)
            runtime_ids = {
                str(event.get("request_contract_id") or event.get("stt_request_contract_id") or "")
                for event in (*by_type["translation_request_contract"], *by_type["stt_request_contract"])
                if event.get("request_contract_id") or event.get("stt_request_contract_id")
            }
            index_ids = {
                str(event.get("request_contract_id") or event.get("stt_request_contract_id") or "")
                for event in indexed if isinstance(event, dict)
            } if isinstance(indexed, list) else set()
            if runtime_ids != index_ids:
                issues.append(_issue("contract_index_mismatch", "contract index IDs differ from runtime JSONL"))
            else:
                runtime_contracts = {
                    str(event.get("request_contract_id") or event.get("stt_request_contract_id")): {
                        key: value for key, value in event.items() if key != "_bundle_ordinal"
                    }
                    for event in (*by_type["translation_request_contract"], *by_type["stt_request_contract"])
                }
                indexed_contracts = {
                    str(event.get("request_contract_id") or event.get("stt_request_contract_id")): event
                    for event in indexed if isinstance(event, dict)
                }
                if runtime_contracts != indexed_contracts:
                    issues.append(_issue("contract_index_content_mismatch", "contract index content differs from runtime JSONL"))
        except (json.JSONDecodeError, ValueError):
            issues.append(_issue("invalid_contract_index", "request_contracts.json is invalid"))

    stt_by_utterance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in by_type["stt"]:
        utterance_id = str(event.get("utterance_id") or "")
        if utterance_id:
            stt_by_utterance[utterance_id].append(event)
        contract_id = str(event.get("stt_request_contract_id") or "")
        if event.get("request_sent") and not contract_id:
            issues.append(_issue("missing_stt_request_contract", "sent STT attempt has no contract reference", event_ordinal=event.get("_bundle_ordinal")))
        elif contract_id and len(stt_contracts.get(contract_id, ())) != 1:
            issues.append(_issue("broken_stt_contract_link", "STT result does not resolve to one contract", event_ordinal=event.get("_bundle_ordinal"), reference=contract_id))
        for text_key, hash_key in (
            ("provider_raw_text", "provider_raw_text_sha256"),
            ("accepted_text", "accepted_text_sha256"),
        ):
            if hash_key in event and str(event.get(hash_key) or "") != sha256_text(str(event.get(text_key) or "")):
                issues.append(_issue("stt_result_hash_mismatch", f"{text_key} hash is invalid", event_ordinal=event.get("_bundle_ordinal"), reference=utterance_id))

    sentences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in by_type["sentence"]:
        sentence_id = str(event.get("sentence_id") or "")
        if sentence_id:
            sentences[sentence_id].append(event)
        utterance_ids = _ids(event, "utterance_id", "source_utterance_ids")
        for utterance_id in utterance_ids:
            if not stt_by_utterance.get(utterance_id):
                issues.append(_issue("broken_sentence_stt_link", "sentence source utterance has no STT event", event_ordinal=event.get("_bundle_ordinal"), reference=utterance_id))
            else:
                successful = [
                    row for row in stt_by_utterance[utterance_id]
                    if row.get("status") == "success"
                    and (row.get("accepted_text") or row.get("text"))
                ]
                if len(successful) > 1:
                    issues.append(_issue("ambiguous_sentence_stt_lineage", "sentence source utterance resolves to multiple successful STT results", severity="warning", event_ordinal=event.get("_bundle_ordinal"), reference=utterance_id))
                elif not successful:
                    issues.append(_issue("unresolved_sentence_stt_result", "sentence source utterance has no uniquely successful STT result", severity="unresolved", event_ordinal=event.get("_bundle_ordinal"), reference=utterance_id))
        if "text" not in event and "source_text" not in event:
            issues.append(_issue("sentence_text_evidence_missing", "sentence event has length but no assembled text; STT-to-sentence content cannot be verified", severity="unresolved", event_ordinal=event.get("_bundle_ordinal"), reference=sentence_id))

    audio_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    audio_path = bundle / "audio_index.json"
    if audio_path.is_file():
        try:
            rows = _json(audio_path)
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("utterance_id"):
                    audio_index[str(row["utterance_id"])].append(row)
        except (json.JSONDecodeError, ValueError):
            issues.append(_issue("invalid_audio_index", "audio_index.json is invalid"))
    else:
        issues.append(_issue("missing_audio_index", "audio_index.json is absent"))

    provisional_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chains: list[dict[str, Any]] = []
    referenced_translation_contracts: set[str] = set()
    for event in by_type["provisional_translation"]:
        provisional_id = str(event.get("provisional_id") or "")
        if provisional_id:
            provisional_by_id[provisional_id].append(event)
        contract_id = str(event.get("request_contract_id") or "")
        if contract_id:
            referenced_translation_contracts.add(contract_id)
            if len(translation_contracts.get(contract_id, ())) != 1:
                issues.append(_issue("missing_translation_request_contract", "provisional provider event contract does not resolve uniquely", event_ordinal=event.get("_bundle_ordinal"), reference=contract_id))
        elif event.get("engine") and str(event.get("action") or "") in {"succeeded", "failed", "guard_rejected"}:
            issues.append(_issue("orphan_provider_attempt", "provisional provider event has no request contract ID", event_ordinal=event.get("_bundle_ordinal"), reference=provisional_id))
    for translation in by_type["translation"]:
        ordinal = int(translation.get("_bundle_ordinal") or 0)
        sentence_id = str(translation.get("sentence_id") or "")
        sentence_rows = sentences.get(sentence_id, []) if sentence_id else []
        utterance_ids = _ids(translation, "utterance_id", "source_utterance_ids")
        if not utterance_ids and len(sentence_rows) == 1:
            utterance_ids = _ids(sentence_rows[0], "utterance_id", "source_utterance_ids")
        chain_issues: list[str] = []
        if not sentence_id or len(sentence_rows) != 1:
            code = "ambiguous_translation_sentence_lineage" if len(sentence_rows) > 1 else "broken_translation_sentence_link"
            issues.append(_issue(code, "translation does not resolve to exactly one sentence", severity="warning" if len(sentence_rows) > 1 else "error", event_ordinal=ordinal, reference=sentence_id))
            chain_issues.append(code)
        stt_rows = [row for uid in utterance_ids for row in stt_by_utterance.get(uid, [])]
        audio_rows = [row for uid in utterance_ids for row in audio_index.get(uid, [])]
        if utterance_ids and len(audio_rows) < len(utterance_ids):
            issues.append(_issue("missing_audio_lineage", "one or more source utterances have no audio index row", severity="unresolved", event_ordinal=ordinal))
            chain_issues.append("missing_audio_lineage")
        elif any(not row.get("bundle_path") for row in audio_rows):
            issues.append(_issue("audio_bytes_missing", "audio lineage is indexed but WAV bytes are not included", severity="unresolved", event_ordinal=ordinal))
            chain_issues.append("audio_bytes_missing")

        attempts = translation.get("attempts") if isinstance(translation.get("attempts"), list) else []
        attempt_rows: list[dict[str, Any]] = []
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            contract_id = str(attempt.get("request_contract_id") or "")
            if contract_id:
                referenced_translation_contracts.add(contract_id)
            resolved = translation_contracts.get(contract_id, []) if contract_id else []
            if not contract_id:
                issues.append(_issue("orphan_provider_attempt", "provider attempt has no request contract ID", event_ordinal=ordinal, reference=str(index)))
                chain_issues.append("orphan_provider_attempt")
            elif len(resolved) != 1:
                issues.append(_issue("missing_translation_request_contract", "provider attempt contract does not resolve uniquely", event_ordinal=ordinal, reference=contract_id))
                chain_issues.append("missing_translation_request_contract")
            guard = attempt.get("output_guard") if isinstance(attempt.get("output_guard"), dict) else {}
            if not guard:
                issues.append(_issue("adjudication_evidence_missing", "provider attempt has no stage-level adjudication ledger", severity="unresolved", event_ordinal=ordinal, reference=str(index)))
                chain_issues.append("adjudication_evidence_missing")
            else:
                stages = guard.get("candidate_stages")
                if not isinstance(stages, dict):
                    issues.append(_issue("adjudication_stages_missing", "adjudication ledger has no candidate stages", severity="unresolved", event_ordinal=ordinal, reference=str(index)))
                    chain_issues.append("adjudication_stages_missing")
                else:
                    for stage_name, stage in stages.items():
                        if not isinstance(stage, dict) or "sha256" not in stage:
                            issues.append(_issue("adjudication_stage_hash_missing", "candidate stage has no verifiable hash", severity="unresolved", event_ordinal=ordinal, reference=f"{index}:{stage_name}"))
                            chain_issues.append("adjudication_stage_hash_missing")
                        elif str(stage.get("sha256") or "") != sha256_text(str(stage.get("text") or "")):
                            issues.append(_issue("adjudication_stage_hash_mismatch", "candidate stage hash is invalid", event_ordinal=ordinal, reference=f"{index}:{stage_name}"))
                            chain_issues.append("adjudication_stage_hash_mismatch")
            attempt_rows.append({
                "attempt_index": index,
                "engine": attempt.get("engine", ""),
                "model": attempt.get("model", ""),
                "status": attempt.get("status", ""),
                "request_contract_id": contract_id,
                "contract_event_ordinal": resolved[0].get("_bundle_ordinal") if len(resolved) == 1 else None,
                "adjudication": {
                    "disposition": guard.get("disposition", ""),
                    "primary_reason": guard.get("primary_reason", guard.get("reason", "")),
                    "rejection_owner": guard.get("rejection_owner", ""),
                    "failed_invariants": guard.get("failed_invariants", []),
                    "candidate_stages": guard.get("candidate_stages", {}),
                } if guard else None,
            })
        top_contract = str(translation.get("request_contract_id") or "")
        if top_contract:
            referenced_translation_contracts.add(top_contract)
            if len(translation_contracts.get(top_contract, ())) != 1:
                issues.append(_issue("missing_translation_request_contract", "selected translation contract does not resolve uniquely", event_ordinal=ordinal, reference=top_contract))
                chain_issues.append("missing_translation_request_contract")
        if translation.get("result_source") == "api" and not attempts:
            issues.append(_issue("missing_provider_attempts", "API translation has no provider attempt ledger", event_ordinal=ordinal))
            chain_issues.append("missing_provider_attempts")
        if translation.get("subtitle_emitted") and not translation.get("target_text"):
            issues.append(_issue("publication_target_missing", "published translation has no target text", event_ordinal=ordinal))
            chain_issues.append("publication_target_missing")
        attribution_gaps = []
        if not utterance_ids:
            attribution_gaps.append("source_utterance_ids_missing")
        if sentence_rows and "text" not in sentence_rows[0] and "source_text" not in sentence_rows[0]:
            attribution_gaps.append("sentence_text_evidence_missing")
        if not attempts and translation.get("result_source") in {"api", "provisional_promotion"}:
            attribution_gaps.append("provider_attempt_evidence_missing")
        provisional_id = str(translation.get("provisional_id") or "")
        provisional_rows = provisional_by_id.get(provisional_id, []) if provisional_id else []
        if provisional_id and not provisional_rows:
            issues.append(_issue("broken_provisional_lineage", "translation references a provisional ID with no lifecycle event", severity="unresolved", event_ordinal=ordinal, reference=provisional_id))
            chain_issues.append("broken_provisional_lineage")
        cache_contract = translation.get("cache_lookup_contract") if isinstance(translation.get("cache_lookup_contract"), dict) else {}
        if cache_contract.get("cache_key_sha256"):
            cache_payload = {
                key: cache_contract.get(key)
                for key in ("prepared_source_text", "incomplete", "prompt_version", "route_id", "history_cohort_id")
            }
            if str(cache_contract["cache_key_sha256"]) != stable_identity(cache_payload):
                issues.append(_issue("cache_key_hash_mismatch", "cache lookup key hash is invalid", event_ordinal=ordinal))
                chain_issues.append("cache_key_hash_mismatch")
        chains.append({
            "chain_id": f"translation-event-{ordinal}",
            "translation_event_ordinal": ordinal,
            "sentence_id": sentence_id,
            "sentence_event_ordinals": [row.get("_bundle_ordinal") for row in sentence_rows],
            "source_utterance_ids": utterance_ids,
            "audio": [{"utterance_id": row.get("utterance_id"), "available": row.get("available"), "bundle_path": row.get("bundle_path")} for row in audio_rows],
            "stt": [{"event_ordinal": row.get("_bundle_ordinal"), "engine": row.get("engine"), "status": row.get("status"), "stt_request_contract_id": row.get("stt_request_contract_id")} for row in stt_rows],
            "provider_attempts": attempt_rows,
            "fallback": {
                "attempted": len(attempt_rows) > 1,
                "attempt_count": len(attempt_rows),
                "selected_route_id": translation.get("route_id", ""),
            },
            "cache": cache_contract,
            "provisional": {
                "provisional_id": provisional_id,
                "result_source": translation.get("result_source", ""),
                "events": [
                    {
                        "event_ordinal": row.get("_bundle_ordinal"),
                        "action": row.get("action", ""),
                        "request_contract_id": row.get("request_contract_id", ""),
                    }
                    for row in provisional_rows
                ],
            },
            "publication": {"status": translation.get("status", ""), "subtitle_emitted": translation.get("subtitle_emitted"), "suppressed_reason": translation.get("subtitle_suppressed_reason", ""), "target_text": translation.get("target_text")},
            "attribution_supported": not attribution_gaps and not chain_issues,
            "attribution_gaps": list(dict.fromkeys((*attribution_gaps, *chain_issues))),
        })

    for contract_id, rows in translation_contracts.items():
        if contract_id not in referenced_translation_contracts and not any(row.get("contract_role") == "available_route_request" for row in rows):
            issues.append(_issue("orphan_translation_contract", "translation contract is not referenced by an attempt or result", severity="warning", reference=contract_id))

    artifact_observations: dict[str, set[str]] = defaultdict(set)
    for contract in (*by_type["translation_request_contract"], *by_type["stt_request_contract"]):
        hashes = contract.get("artifact_hashes")
        if not isinstance(hashes, dict):
            issues.append(_issue("missing_artifact_hashes", "request contract has no artifact hash manifest", severity="unresolved", event_ordinal=contract.get("_bundle_ordinal")))
            continue
        for name, digest in hashes.items():
            if not digest:
                issues.append(_issue("missing_artifact_hash", "referenced artifact has no recorded hash", severity="unresolved", event_ordinal=contract.get("_bundle_ordinal"), reference=str(name)))
            else:
                artifact_observations[str(name)].add(str(digest))
                if len(str(digest)) != 64 or any(char not in "0123456789abcdef" for char in str(digest).lower()):
                    issues.append(_issue("invalid_artifact_hash", "artifact hash is not a SHA-256 digest", event_ordinal=contract.get("_bundle_ordinal"), reference=str(name)))
    for name, digests in artifact_observations.items():
        if len(digests) > 1:
            issues.append(_issue("ambiguous_artifact_version", "same artifact path has multiple hashes in one bundle", severity="warning", reference=name))
        issues.append(_issue("artifact_content_not_bundled", "artifact hash is recorded but bytes are absent; content cannot be independently verified", severity="unresolved", reference=name))

    counts: dict[str, int] = defaultdict(int)
    for issue in issues:
        counts[str(issue["severity"])] += 1
    return {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "bundle_path": str(bundle.resolve()),
        "run_id": manifest.get("run_id", ""),
        "event_count": len(events),
        "chain_count": len(chains),
        "integrity_ok": not any(issue["severity"] == "error" for issue in issues),
        "independent_root_cause_attribution_supported": bool(chains) and all(chain["attribution_supported"] for chain in chains),
        "issue_counts": dict(sorted(counts.items())),
        "issues": issues,
        "chains": chains,
    }


def render_forensics_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Runtime forensics: `{report.get('run_id') or 'unknown'}`",
        "",
        f"- Integrity: `{'PASS' if report.get('integrity_ok') else 'FAIL'}`",
        f"- Causal chains: `{report.get('chain_count', 0)}`",
        f"- Independent root-cause attribution: `{'SUPPORTED' if report.get('independent_root_cause_attribution_supported') else 'UNRESOLVED'}`",
        f"- Issues: `{report.get('issue_counts', {})}`",
        "",
        "## Causal chains",
        "",
    ]
    for chain in report.get("chains", []):
        lines.append(
            f"- `{chain['chain_id']}`: audio={len(chain['audio'])}, STT={len(chain['stt'])}, "
            f"sentence=`{chain['sentence_id'] or 'missing'}`, attempts={len(chain['provider_attempts'])}, "
            f"published={chain['publication']['subtitle_emitted']}, attribution={'yes' if chain['attribution_supported'] else 'unresolved'}"
        )
    lines += ["", "## Findings", ""]
    for issue in report.get("issues", []):
        location = f" event={issue['event_ordinal']}" if issue.get("event_ordinal") else ""
        reference = f" ref=`{issue['reference']}`" if issue.get("reference") else ""
        lines.append(f"- **{issue['severity'].upper()}** `{issue['code']}`:{location}{reference} {issue['message']}")
    if not report.get("issues"):
        lines.append("- No integrity or lineage findings.")
    return "\n".join(lines) + "\n"
