from __future__ import annotations

import copy
import json

import pytest

from scripts.rebuild_semantic_quality_provenance import rebuild_provenance


RUN_ID = "run-a"


def _translation(
    sequence_id: int,
    *,
    source_ids: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    source_text: str | None = None,
):
    return {
        "schema_version": 3,
        "event_type": "translation",
        "run_id": RUN_ID,
        "sequence_id": sequence_id,
        "created_at": f"2026-08-02T00:00:{sequence_id:02d}+00:00",
        "profile_id": "url",
        "status": "success",
        "source_text": source_text or f"source-{sequence_id}",
        "target_text": f"target-{sequence_id}",
        "source_utterance_ids": source_ids or [],
        "evidence_source_utterance_ids": evidence_ids or [],
    }


def _stt(utterance_id: str, *, status: str = "success"):
    return {
        "schema_version": 3,
        "event_type": "stt",
        "run_id": RUN_ID,
        "utterance_id": utterance_id,
        "status": status,
    }


def _row(event, line_no):
    return {"path": "events.jsonl", "line_no": line_no, "event": event}


def _runtime_ref(event, *, selection_status="selected"):
    return {
        "run_id": event["run_id"],
        "translation_sequence_id": event["sequence_id"],
        "runtime_created_at_utc": event["created_at"],
        "annotation_local_time": "08:00:00",
        "profile_id": event["profile_id"],
        "status": event["status"],
        "source_text": event["source_text"],
        "target_text": event["target_text"],
        "source_utterance_ids": [],
        "audio_refs": [],
        "source_similarity": 0.1,
        "target_similarity": 0.1,
        "combined_similarity": 0.1,
        "selection_status": selection_status,
    }


def _manifest(*runtime_refs):
    return {
        "schema_version": 1,
        "matching_contract": {"disambiguation": "frozen-sentinel"},
        "direct_annotation_linked_missing_source_id_count": 1,
        "annotations": [
            {
                "annotation_id": 68,
                "timestamp_matches": [
                    {
                        "match_status": "text_disambiguated",
                        "candidate_count": len(runtime_refs),
                        "runtime_refs": list(runtime_refs),
                    }
                ],
                "provenance_status": "partial_selected_translation_missing_source_ids",
            }
        ],
    }


def _write_wavs(audio_root, *utterance_ids):
    run_dir = audio_root / RUN_ID
    run_dir.mkdir(parents=True)
    for utterance_id in utterance_ids:
        (run_dir / f"{utterance_id}.wav").write_bytes(b"RIFF-test")


def test_rebuilds_pure_residual_from_evidence_ids(tmp_path):
    event = _translation(1, evidence_ids=["utt-1", "utt-2"])
    manifest = _manifest(_runtime_ref(event))
    _write_wavs(tmp_path, "utt-1", "utt-2")

    rebuilt = rebuild_provenance(
        manifest,
        [_row(event, 1), _row(_stt("utt-1"), 2), _row(_stt("utt-2"), 3)],
        audio_root=tmp_path,
        project_root=tmp_path,
    )

    ref = rebuilt["annotations"][0]["timestamp_matches"][0]["runtime_refs"][0]
    assert rebuilt["schema_version"] == 2
    assert rebuilt["direct_annotation_linked_missing_source_id_count"] == 0
    assert rebuilt["annotations"][0]["provenance_status"] == "runtime_translation_candidates_linked"
    assert ref["source_utterance_ids"] == []
    assert ref["evidence_source_utterance_ids"] == ["utt-1", "utt-2"]
    assert [(row["utterance_id"], row["source_kind"]) for row in ref["audio_refs"]] == [
        ("utt-1", "evidence"),
        ("utt-2", "evidence"),
    ]


def test_orders_current_before_evidence_dedupes_and_current_wins(tmp_path):
    event = _translation(
        1,
        source_ids=["utt-2", "utt-1", "utt-2"],
        evidence_ids=["utt-1", "utt-3", "utt-3"],
    )
    _write_wavs(tmp_path, "utt-1", "utt-2", "utt-3")
    rows = [_row(event, 1)] + [
        _row(_stt(utterance_id), index)
        for index, utterance_id in enumerate(("utt-1", "utt-2", "utt-3"), start=2)
    ]

    rebuilt = rebuild_provenance(
        _manifest(_runtime_ref(event)), rows, audio_root=tmp_path, project_root=tmp_path
    )

    refs = rebuilt["annotations"][0]["timestamp_matches"][0]["runtime_refs"][0]["audio_refs"]
    assert [(row["utterance_id"], row["source_kind"]) for row in refs] == [
        ("utt-2", "current"),
        ("utt-1", "current"),
        ("utt-3", "evidence"),
    ]


def test_preserves_frozen_selection_instead_of_rescoring(tmp_path):
    selected = _translation(1, source_ids=["utt-1"], source_text="low similarity")
    rejected = _translation(2, source_ids=["utt-2"], source_text="perfect annotation text")
    manifest = _manifest(
        _runtime_ref(selected, selection_status="selected"),
        _runtime_ref(rejected, selection_status="not_selected"),
    )
    original_selection = [
        ref["selection_status"]
        for ref in manifest["annotations"][0]["timestamp_matches"][0]["runtime_refs"]
    ]
    _write_wavs(tmp_path, "utt-1", "utt-2")

    rebuilt = rebuild_provenance(
        manifest,
        [
            _row(selected, 1),
            _row(rejected, 2),
            _row(_stt("utt-1"), 3),
            _row(_stt("utt-2"), 4),
        ],
        audio_root=tmp_path,
        project_root=tmp_path,
    )

    match = rebuilt["annotations"][0]["timestamp_matches"][0]
    assert match["candidate_count"] == 2
    assert match["match_status"] == "text_disambiguated"
    assert [ref["selection_status"] for ref in match["runtime_refs"]] == original_selection
    assert rebuilt["matching_contract"]["disambiguation"] == "frozen-sentinel"


def test_keeps_missing_status_when_both_provenance_lists_are_empty(tmp_path):
    event = _translation(1)

    rebuilt = rebuild_provenance(
        _manifest(_runtime_ref(event)),
        [_row(event, 1)],
        audio_root=tmp_path,
        project_root=tmp_path,
    )

    assert rebuilt["direct_annotation_linked_missing_source_id_count"] == 1
    assert (
        rebuilt["annotations"][0]["provenance_status"]
        == "partial_selected_translation_missing_source_ids"
    )


@pytest.mark.parametrize("translation_rows", [[], [1, 1]])
def test_fails_on_missing_or_duplicate_translation_tuple(tmp_path, translation_rows):
    event = _translation(1, source_ids=["utt-1"])
    rows = [_row(event, line_no) for line_no in translation_rows]

    with pytest.raises(ValueError, match="expected one schema-v3 translation"):
        rebuild_provenance(
            _manifest(_runtime_ref(event)), rows, audio_root=tmp_path, project_root=tmp_path
        )


def test_fails_on_mismatched_frozen_runtime_field(tmp_path):
    event = _translation(1, source_ids=["utt-1"])
    frozen_ref = _runtime_ref(event)
    frozen_ref["source_text"] = "changed frozen text"

    with pytest.raises(ValueError, match="frozen runtime field mismatch"):
        rebuild_provenance(
            _manifest(frozen_ref), [_row(event, 1)], audio_root=tmp_path, project_root=tmp_path
        )


@pytest.mark.parametrize(
    "stt_rows, error",
    [
        ([], "found 0"),
        (["success", "success"], "found 2"),
        (["failed"], "found 0"),
    ],
)
def test_fails_on_absent_ambiguous_or_non_success_stt(tmp_path, stt_rows, error):
    event = _translation(1, source_ids=["utt-1"])
    _write_wavs(tmp_path, "utt-1")
    rows = [_row(event, 1)] + [
        _row(_stt("utt-1", status=status), index)
        for index, status in enumerate(stt_rows, start=2)
    ]

    with pytest.raises(ValueError, match=error):
        rebuild_provenance(
            _manifest(_runtime_ref(event)), rows, audio_root=tmp_path, project_root=tmp_path
        )


def test_fails_when_wav_is_missing(tmp_path):
    event = _translation(1, source_ids=["utt-1"])

    with pytest.raises(ValueError, match="missing WAV"):
        rebuild_provenance(
            _manifest(_runtime_ref(event)),
            [_row(event, 1), _row(_stt("utt-1"), 2)],
            audio_root=tmp_path,
            project_root=tmp_path,
        )
