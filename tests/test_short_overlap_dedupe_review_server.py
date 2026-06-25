import hashlib
import json
import wave

import pytest

from scripts.short_overlap_dedupe_review_server import AnnotationStore, build_review_tasks


def _write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)


def test_annotation_store_persists_decision(tmp_path):
    shadow_path = tmp_path / "shadow.json"
    shadow_path.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    annotation_path = tmp_path / "annotations.json"
    tasks = [{"candidate_id": "D001"}]
    store = AnnotationStore(annotation_path, shadow_path, tasks)

    result = store.update({"candidate_id": "D001", "decision": "safe_dedupe", "notes": "same audio"})

    assert result["shadow_path"] == str(shadow_path.resolve())
    assert result["annotations"]["D001"]["decision"] == "safe_dedupe"
    assert json.loads(annotation_path.read_text(encoding="utf-8"))["annotations"]["D001"]["notes"] == "same audio"
    with pytest.raises(ValueError, match="unknown decision"):
        store.update({"candidate_id": "D001", "decision": "bad"})


def test_annotation_store_rejects_changed_shadow(tmp_path):
    shadow_path = tmp_path / "shadow.json"
    shadow_path.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    annotation_path = tmp_path / "annotations.json"
    AnnotationStore(annotation_path, shadow_path, [{"candidate_id": "D001"}]).update(
        {"candidate_id": "D001", "decision": "unclear"}
    )
    shadow_path.write_text(json.dumps({"candidates": [{"changed": True}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="different shadow artifact"):
        AnnotationStore(annotation_path, shadow_path, [{"candidate_id": "D001"}])


def test_build_review_tasks_uses_boundary_audio(monkeypatch, tmp_path):
    import scripts.short_overlap_dedupe_review_server as module

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    previous = tmp_path / "logs" / "audio_dump" / "run-1" / "utt-1.wav"
    current = tmp_path / "logs" / "audio_dump" / "run-1" / "utt-2.wav"
    _write_wav(previous)
    _write_wav(current)
    shadow = {
        "candidates": [
            {
                "run_id": "run-1",
                "previous_last_source_utterance_id": "utt-1",
                "first_source_utterance_id": "utt-2",
                "removed_prefix": "메이플?",
                "previous_source_text": "안해... 메이플?",
                "current_source_text": "메이플? 그거잖아.",
                "shadow_source_text": "그거잖아.",
            }
        ]
    }

    tasks, audio_map = build_review_tasks(shadow)

    assert tasks[0]["candidate_id"] == "D001"
    assert tasks[0]["previous_audio"]["utterance_id"] == "utt-1"
    assert tasks[0]["current_audio"]["utterance_id"] == "utt-2"
    assert tasks[0]["removed_prefix"] == "메이플?"
    assert tasks[0]["current_romanization"] == "me-i-peul? geu-geo-jan-a."
    assert audio_map["D001-current"][2] == hashlib.sha256(current.read_bytes()).hexdigest()


def test_build_review_tasks_filters_sensitivity_union_to_min4(monkeypatch, tmp_path):
    import scripts.short_overlap_dedupe_review_server as module

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    audio_root = tmp_path / "logs" / "audio_dump" / "run-1"
    for utterance_id in ("utt-1", "utt-2", "utt-3"):
        _write_wav(audio_root / f"{utterance_id}.wav")
    shadow = {
        "candidates": [
            {
                "run_id": "run-1",
                "previous_last_source_utterance_id": "utt-1",
                "first_source_utterance_id": "utt-2",
                "removed_prefix": "메이플?",
                "removed_prefix_min4": "메이플?",
                "previous_source_text": "그래 메이플?",
                "current_source_text": "메이플? 그거잖아.",
                "shadow_source_text_min4": "그거잖아.",
                "definitions": {"blunt_min4": True, "aggressive_min3": True},
            },
            {
                "run_id": "run-1",
                "previous_last_source_utterance_id": "utt-2",
                "first_source_utterance_id": "utt-3",
                "removed_prefix": "그리고",
                "removed_prefix_min4": "",
                "previous_source_text": "말하고 그리고",
                "current_source_text": "그리고 다음 말",
                "shadow_source_text_min4": "그리고 다음 말",
                "definitions": {"blunt_min4": False, "aggressive_min3": True},
            },
        ]
    }

    tasks, _ = build_review_tasks(shadow)

    assert len(tasks) == 1
    assert tasks[0]["removed_prefix"] == "메이플?"
    assert tasks[0]["shadow_source_text"] == "그거잖아."


def test_build_review_tasks_skips_candidate_with_missing_boundary_audio(monkeypatch, tmp_path):
    import scripts.short_overlap_dedupe_review_server as module

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    current = tmp_path / "logs" / "audio_dump" / "run-1" / "utt-2.wav"
    _write_wav(current)
    shadow = {
        "candidates": [
            {
                "run_id": "run-1",
                "previous_last_source_utterance_id": "utt-1",
                "first_source_utterance_id": "utt-2",
                "removed_prefix": "메이플?",
                "previous_source_text": "그래 메이플?",
                "current_source_text": "메이플? 그거잖아.",
                "shadow_source_text": "그거잖아.",
            }
        ]
    }

    tasks, audio_map = build_review_tasks(shadow)

    assert tasks == []
    assert audio_map == {}
