import hashlib
import json
from pathlib import Path

from modules.forensics_contract import message_manifest, sha256_text, stable_identity
from utils.forensics_analysis import analyze_forensics_bundle, render_forensics_report
from scripts.analyze_forensics_bundle import main as analyzer_main


def _write_bundle(tmp_path: Path, *, corrupt: set[str] | None = None) -> Path:
    corrupt = corrupt or set()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    messages = (("system", "translate"), ("user", "안녕"))
    stt_contract = {
        "schema_version": 6,
        "event_type": "stt_request_contract",
        "run_id": "run-1",
        "stt_request_contract_id": "stt-c1",
        "utterance_id": "utt-1",
        "engine": "elevenlabs",
        "request_parameters": {"language_code": "ko", "keyterms": ["이름"]},
        "keyterms_sha256": stable_identity({"keyterms": ["이름"]}),
        "artifact_hashes": {},
    }
    translation_contract = {
        "schema_version": 6,
        "event_type": "translation_request_contract",
        "run_id": "run-1",
        "request_contract_id": "tr-c1",
        "contract_role": "available_route_request",
        "provider_source": "안녕",
        "provider_source_sha256": sha256_text("안녕"),
        "effective_system_prompt": "translate",
        "effective_system_prompt_sha256": sha256_text("translate"),
        "messages": list(message_manifest(messages)),
        "messages_sha256": stable_identity({"messages": list(messages)}),
        "artifact_hashes": {},
    }
    if "message_hash" in corrupt:
        translation_contract["messages"][1]["content_sha256"] = "bad"
    if "source_hash" in corrupt:
        translation_contract["provider_source_sha256"] = "bad"
    events = [
        stt_contract,
        {
            "schema_version": 6,
            "event_type": "stt",
            "run_id": "run-1",
            "utterance_id": "utt-1",
            "request_sent": True,
            "status": "success",
            "engine": "elevenlabs",
            "stt_request_contract_id": "missing" if "stt_link" in corrupt else "stt-c1",
            "provider_raw_text": "안녕",
            "provider_raw_text_sha256": "bad" if "stt_result_hash" in corrupt else sha256_text("안녕"),
            "accepted_text": "안녕",
            "accepted_text_sha256": sha256_text("안녕"),
        },
        {
            "schema_version": 6,
            "event_type": "sentence",
            "run_id": "run-1",
            "sentence_id": "sentence-1",
            "source_utterance_ids": ["utt-1"],
            "text_len": 2,
        },
        translation_contract,
        {
            "schema_version": 6,
            "event_type": "translation",
            "run_id": "run-1",
            "sentence_id": "sentence-1",
            "source_utterance_ids": ["utt-1"],
            "request_contract_id": "tr-c1",
            "result_source": "api",
            "status": "success",
            "target_text": "你好",
            "subtitle_emitted": True,
            "attempts": [{
                "engine": "deepseek",
                "status": "success",
                "request_contract_id": "" if "orphan_attempt" in corrupt else "tr-c1",
                "output_guard": {
                    "disposition": "accepted",
                    "candidate_stages": {
                        "raw_provider": {"text": "你好", "sha256": "bad" if "stage_hash" in corrupt else sha256_text("你好")},
                        "protection_restored": {"text": "你好", "sha256": sha256_text("你好")},
                        "source_corrected": {"text": "你好", "sha256": sha256_text("你好")},
                    },
                    "failed_invariants": [],
                },
            }],
            "cache_lookup_contract": {"lookup_result": "miss"},
        },
    ]
    if "ambiguous_sentence" in corrupt:
        events.insert(3, dict(events[2]))
    event_path = bundle / "runtime_events.jsonl"
    event_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events), encoding="utf-8")
    contracts = json.loads(json.dumps([stt_contract, translation_contract], ensure_ascii=False))
    if "contract_index" in corrupt:
        contracts = [stt_contract]
    if "contract_index_content" in corrupt:
        contracts[1]["provider_source"] = "different"
    (bundle / "request_contracts.json").write_text(json.dumps(contracts, ensure_ascii=False), encoding="utf-8")
    (bundle / "audio_index.json").write_text(json.dumps([{"utterance_id": "utt-1", "available": False, "bundle_path": None}]), encoding="utf-8")
    integrity = {}
    for name in ("runtime_events.jsonl", "request_contracts.json", "audio_index.json"):
        path = bundle / name
        integrity[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
    if "artifact_hash" in corrupt:
        integrity["audio_index.json"]["sha256"] = "bad"
    manifest = {
        "bundle_schema_version": 2,
        "run_id": "run-1",
        "runtime_event_files": ["runtime_events.jsonl"],
        "integrity": integrity,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_valid_bundle_builds_complete_structural_chain_and_reports_evidence_gap(tmp_path):
    report = analyze_forensics_bundle(_write_bundle(tmp_path))

    assert report["integrity_ok"] is True
    assert report["chain_count"] == 1
    chain = report["chains"][0]
    assert chain["source_utterance_ids"] == ["utt-1"]
    assert chain["stt"][0]["stt_request_contract_id"] == "stt-c1"
    assert chain["provider_attempts"][0]["request_contract_id"] == "tr-c1"
    assert chain["provider_attempts"][0]["adjudication"]["candidate_stages"]["raw_provider"]["text"] == "你好"
    assert chain["publication"]["subtitle_emitted"] is True
    assert chain["attribution_supported"] is False
    assert "sentence_text_evidence_missing" in chain["attribution_gaps"]
    assert "sentence_text_evidence_missing" in _codes(report)

    human = render_forensics_report(report)
    assert "translation-event-5" in human
    assert "UNRESOLVED" in human


def test_corrupted_bundle_reports_hash_contract_and_orphan_failures(tmp_path):
    report = analyze_forensics_bundle(
        _write_bundle(
            tmp_path,
            corrupt={"message_hash", "source_hash", "stt_link", "stt_result_hash", "stage_hash", "orphan_attempt", "contract_index", "artifact_hash"},
        )
    )
    codes = _codes(report)

    assert report["integrity_ok"] is False
    assert {
        "message_hash_mismatch",
        "provider_source_hash_mismatch",
        "broken_stt_contract_link",
        "orphan_provider_attempt",
        "contract_index_mismatch",
        "bundle_artifact_hash_mismatch",
        "stt_result_hash_mismatch",
        "adjudication_stage_hash_mismatch",
    }.issubset(codes)


def test_ambiguous_sentence_lineage_is_never_silently_selected(tmp_path):
    report = analyze_forensics_bundle(
        _write_bundle(tmp_path, corrupt={"ambiguous_sentence"})
    )

    assert "ambiguous_translation_sentence_lineage" in _codes(report)
    assert report["chains"][0]["attribution_supported"] is False
    assert len(report["chains"][0]["sentence_event_ordinals"]) == 2


def test_contract_index_content_must_match_runtime_source_of_truth(tmp_path):
    report = analyze_forensics_bundle(
        _write_bundle(tmp_path, corrupt={"contract_index_content"})
    )

    assert "contract_index_content_mismatch" in _codes(report)
    assert report["integrity_ok"] is False


def test_cli_writes_machine_and_human_reports(tmp_path):
    bundle = _write_bundle(tmp_path)
    json_output = tmp_path / "report.json"
    markdown_output = tmp_path / "report.md"

    exit_code = analyzer_main([
        str(bundle),
        "--json-output", str(json_output),
        "--report-output", str(markdown_output),
    ])

    assert exit_code == 0
    assert json.loads(json_output.read_text(encoding="utf-8"))["chain_count"] == 1
    assert "# Runtime forensics" in markdown_output.read_text(encoding="utf-8")
