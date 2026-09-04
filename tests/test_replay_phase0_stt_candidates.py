import json
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

import scripts.replay_phase0_stt_candidates as replay_module
from scripts.replay_phase0_stt_candidates import (
    _faster_whisper_generator,
    clean_sensevoice_text,
    main,
    parse_args,
    replay_cases,
    select_cases,
    verify_audio_asset,
)


def test_clean_sensevoice_text_extracts_metadata_tokens():
    text, tags = clean_sensevoice_text(
        "<|ko|><|EMO_UNKNOWN|><|Speech|><|withitn|>맞아 어 맞아"
    )

    assert text == "맞아 어 맞아"
    assert tags == ["<|ko|>", "<|EMO_UNKNOWN|>", "<|Speech|>", "<|withitn|>"]


def test_select_cases_limits_groups_and_ids():
    manifest = {
        "cases": [
            {"sample_id": "S001", "root_cause_group": "clean_host_stt"},
            {"sample_id": "S002", "root_cause_group": "source_routing"},
        ]
    }

    selected = select_cases(manifest, groups={"clean_host_stt"}, case_ids={"S001"})

    assert [case["sample_id"] for case in selected] == ["S001"]


def test_replay_cases_orders_audio_and_preserves_candidates(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    sf.write(first, np.zeros(1600, dtype=np.float32), 16000)
    sf.write(second, np.ones(3200, dtype=np.float32) * 0.01, 16000)
    case = {
        "sample_id": "S001",
        "root_cause_group": "clean_host_stt",
        "annotation": {"label": "b_stt_error"},
        "audio_assets": [
            {
                "utterance_id": "utt-2",
                "source_kind": "current",
                "audio_path": str(second),
                "size_bytes": second.stat().st_size,
                "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
            },
            {
                "utterance_id": "utt-1",
                "source_kind": "evidence",
                "audio_path": str(first),
                "size_bytes": first.stat().st_size,
                "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
            },
        ],
        "sample": {
            "source_text": "groq",
            "source_chunks": [
                {"utterance_id": "utt-2", "stt_event_line": 20},
                {"utterance_id": "utt-1", "stt_event_line": 10},
            ],
        },
    }
    outputs = iter(
        [
            "<|ko|><|Speech|>first",
            "<|ko|><|Speech|>second",
        ]
    )

    result = replay_cases([case], generate=lambda _audio: next(outputs), project_root=tmp_path)[0]

    assert [row["utterance_id"] for row in result["sensevoice_chunks"]] == ["utt-1", "utt-2"]
    assert result["sensevoice_text"] == "second"
    assert result["sensevoice_evidence_text"] == "first"
    assert result["sensevoice_chunks"][0]["audio_fingerprint_verified"] is True


def test_replay_cases_keeps_faster_whisper_current_and_evidence_separate(tmp_path):
    current = tmp_path / "current.wav"
    evidence = tmp_path / "evidence.wav"
    sf.write(current, np.zeros(1600, dtype=np.float32), 16000)
    sf.write(evidence, np.zeros(1600, dtype=np.float32), 16000)
    case = {
        "sample_id": "T25-001",
        "root_cause_group": "audio_replay_required",
        "audio_assets": [
            {
                "utterance_id": "utt-current",
                "source_kind": "current",
                "audio_path": str(current),
                "size_bytes": current.stat().st_size,
                "sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
            },
            {
                "utterance_id": "utt-evidence",
                "source_kind": "evidence",
                "audio_path": str(evidence),
                "size_bytes": evidence.stat().st_size,
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            },
        ],
        "sample": {"source_text": "groq", "source_chunks": []},
    }
    outputs = iter(["current candidate", "evidence candidate"])

    result = replay_cases(
        [case],
        generate=lambda _audio: next(outputs),
        project_root=tmp_path,
        engine_name="faster_whisper",
    )[0]

    assert result["candidate_text"] == "current candidate"
    assert result["faster_whisper_current_text"] == "current candidate"
    assert result["faster_whisper_evidence_text"] == "evidence candidate"
    assert "sensevoice_text" not in result


def test_faster_whisper_generator_freezes_cpu_replay_parameters(monkeypatch, tmp_path):
    calls = {}

    class FakeModel:
        def __init__(self, model_name, **kwargs):
            calls["init"] = (model_name, kwargs)

        def transcribe(self, audio, **kwargs):
            calls["transcribe"] = (audio, kwargs)
            return iter([SimpleNamespace(text=" 첫째"), SimpleNamespace(text=" 둘째")]), object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    generate = _faster_whisper_generator(
        model_name="large-v3-turbo",
        device="cpu",
        compute_type="int8",
        cpu_threads=6,
        num_workers=1,
        download_root=tmp_path,
        local_files_only=True,
    )

    assert generate(np.zeros(1600, dtype=np.float32)) == "첫째 둘째"
    assert calls["init"] == (
        "large-v3-turbo",
        {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 6,
            "num_workers": 1,
            "download_root": str(tmp_path),
            "local_files_only": True,
        },
    )
    assert calls["transcribe"][1] == {
        "language": "ko",
        "beam_size": 5,
        "temperature": 0.0,
        "vad_filter": False,
        "condition_on_previous_text": False,
        "word_timestamps": False,
    }


def test_default_cli_remains_sensevoice_compatible():
    args = parse_args([])

    assert args.engine == "sensevoice"
    assert args.model is None
    assert args.device is None


def test_default_cli_writes_legacy_sensevoice_aliases(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "output.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "sample_id": "S001",
                        "root_cause_group": "clean_host_stt",
                        "audio_assets": [
                            {
                                "utterance_id": "utt-1",
                                "source_kind": "current",
                                "audio_path": str(audio_path),
                                "size_bytes": audio_path.stat().st_size,
                                "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                            }
                        ],
                        "sample": {"source_text": "groq", "source_chunks": []},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        replay_module,
        "_sensevoice_generator",
        lambda **_kwargs: lambda _audio: "<|ko|><|Speech|>legacy",
    )

    assert main(["--manifest", str(manifest_path), "--output", str(output_path)]) == 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["phase0_stt_replay_schema"] == 2
    assert output["engine"] == "sensevoice"
    assert output["model"] == "iic/SenseVoiceSmall"
    assert output["device"] == "cuda"
    assert output["runtime_versions"]["python"]
    assert output["runtime_versions"]["engine_package"]
    assert output["cases"][0]["sensevoice_text"] == "legacy"
    assert output["cases"][0]["candidate_text"] == "legacy"


def test_main_preflights_before_model_load_and_preserves_existing_output(monkeypatch, tmp_path):
    missing_audio = tmp_path / "missing.wav"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "existing.json"
    output_path.write_text("known-good", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "sample_id": "S001",
                        "root_cause_group": "clean_host_stt",
                        "audio_assets": [
                            {
                                "utterance_id": "utt-1",
                                "source_kind": "current",
                                "audio_path": str(missing_audio),
                                "size_bytes": 1,
                                "sha256": "0" * 64,
                            }
                        ],
                        "sample": {"source_text": "groq", "source_chunks": []},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    model_loaded = False

    def fail_if_loaded(**_kwargs):
        nonlocal model_loaded
        model_loaded = True
        raise AssertionError("model must not load before audio preflight")

    monkeypatch.setattr(replay_module, "_sensevoice_generator", fail_if_loaded)

    assert main(["--manifest", str(manifest_path), "--output", str(output_path)]) == 1
    assert model_loaded is False
    assert output_path.read_text(encoding="utf-8") == "known-good"


def test_verify_audio_asset_rejects_changed_wav(tmp_path):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"before")
    asset = {
        "size_bytes": audio_path.stat().st_size,
        "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
    }
    audio_path.write_bytes(b"after!")

    try:
        verify_audio_asset(asset, audio_path)
    except ValueError as exc:
        assert "sha256 changed" in str(exc)
    else:
        raise AssertionError("changed audio fingerprint was accepted")


def test_t25_manifest_freezes_current_and_evidence_assets():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "data" / "t25_stt_replay_manifest_20260802.json").read_text(encoding="utf-8")
    )

    assert manifest["case_count"] == 21
    assert manifest["current_asset_count"] == 31
    assert manifest["evidence_asset_count"] == 3
    assert manifest["asset_count"] == 34
    assert manifest["audio_seconds"] == 223.688
    assert [case["annotation"]["source_annotation_id"] for case in manifest["cases"]] == [
        49, 53, 55, 56, 57, 61, 62, 63, 67, 70, 73, 74, 78, 81, 83, 84, 86, 87, 90, 91, 92
    ]

    cases = {case["sample_id"]: case for case in manifest["cases"]}
    assert cases["T25-083"]["sample"]["translation_sequence_ids"] == [105]
    assert cases["T25-091"]["sample"]["translation_sequence_ids"] == [216]
    assert cases["T25-091"]["sample"]["evidence_source_utterance_ids"] == ["utt-273"]

    assets = [asset for case in manifest["cases"] for asset in case["audio_assets"]]
    assert len({asset["audio_path"] for asset in assets}) == 34
    assert sum(asset["source_kind"] == "current" for asset in assets) == 31
    assert sum(asset["source_kind"] == "evidence" for asset in assets) == 3
    for asset in assets:
        verify_audio_asset(asset, root / asset["audio_path"])


def test_t25_provenance_freezes_disambiguation_and_raw_annotations():
    root = Path(__file__).resolve().parents[1]
    evidence = json.loads(
        (root / "data" / "semantic_quality_evidence_20260802.json").read_text(
            encoding="utf-8"
        )
    )
    annotations = {row["annotation_id"]: row for row in evidence["annotations"]}

    assert evidence["schema_version"] == 2
    assert evidence["timestamp_match_status_counts"] == {
        "unique_timestamp_candidate": 101,
        "text_disambiguated": 8,
        "ambiguous_same_second": 0,
    }
    assert evidence["direct_annotation_linked_missing_source_id_count"] == 0
    for annotation_id, selected_sequence in (
        (9, 68),
        (28, 142),
        (60, 220),
        (65, 243),
        (74, 122),
        (83, 105),
        (86, 132),
        (91, 216),
    ):
        refs = [
            ref
            for match in annotations[annotation_id]["timestamp_matches"]
            if match["candidate_count"] > 1
            for ref in match["runtime_refs"]
        ]
        assert [ref["translation_sequence_id"] for ref in refs if ref["selection_status"] == "selected"] == [
            selected_sequence
        ]

    annotation_68 = annotations[68]
    selected_refs = [
        ref
        for match in annotation_68["timestamp_matches"]
        for ref in match["runtime_refs"]
        if ref["selection_status"] == "selected"
    ]
    residual_ref = next(ref for ref in selected_refs if ref["translation_sequence_id"] == 20)
    assert annotation_68["provenance_status"] == "runtime_translation_candidates_linked"
    assert residual_ref["source_utterance_ids"] == []
    assert residual_ref["evidence_source_utterance_ids"] == ["utt-26", "utt-27"]
    assert [
        (audio_ref["utterance_id"], audio_ref["source_kind"], audio_ref["wav_status"])
        for audio_ref in residual_ref["audio_refs"]
    ] == [("utt-26", "evidence", "exists"), ("utt-27", "evidence", "exists")]

    raw_sha256 = hashlib.sha256(
        (root / "data" / "manual_quality_annotations_20260802.json").read_bytes()
    ).hexdigest()
    assert raw_sha256 == "a18e20c070ee6d0ea19d67fbe9d1c94171890cad4360b1419b6769e0657a88e8"


def test_t25_dual_asr_artifacts_remain_byte_identical_after_provenance_rebuild():
    root = Path(__file__).resolve().parents[1]
    expected = {
        "t25_stt_replay_manifest_20260802.json":
            "6d4afaf8ac1d3953f2b03bf27ef4a1f96f0148a905e6f2fd7394d21ae4261745",
        "t25_sensevoice_shadow_20260802.json":
            "4fe561c39d856e52356f4321ac7b6dc27a84528f878fd832a1d43740bbe90d13",
        "t25_faster_whisper_shadow_20260802.json":
            "a800458355eb0780f9befea5c2d6ab1bb5e9a599f6fb606e0765aef43ddc6ff3",
    }
    assert {
        filename: hashlib.sha256((root / "data" / filename).read_bytes()).hexdigest()
        for filename in expected
    } == expected


def test_t25_dual_asr_results_match_the_frozen_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "data" / "t25_stt_replay_manifest_20260802.json"
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    for filename, engine in (
        ("t25_sensevoice_shadow_20260802.json", "sensevoice"),
        ("t25_faster_whisper_shadow_20260802.json", "faster_whisper"),
    ):
        result = json.loads((root / "data" / filename).read_text(encoding="utf-8"))
        chunks = [chunk for case in result["cases"] for chunk in case["candidate_chunks"]]

        assert result["engine"] == engine
        assert result["manifest_sha256"] == manifest_sha256
        assert result["case_count"] == 21
        assert len(chunks) == 34
        assert sum(chunk["source_kind"] == "current" for chunk in chunks) == 31
        assert sum(chunk["source_kind"] == "evidence" for chunk in chunks) == 3
        assert all(case["candidate_current_text"] for case in result["cases"])
