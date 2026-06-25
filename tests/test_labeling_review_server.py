import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts.labeling_review_server import (
    AnnotationStore,
    LabelingHTTPServer,
    build_audio_map,
    default_annotation_path,
    load_sample,
    public_sample_data,
    romanize_korean_text,
)


def _sample_data(audio_path):
    return {
        "labeling_sample_schema": 1,
        "speaker_policy": "host-primary",
        "annotation_goal": "Judge host-primary subtitles.",
        "annotation_rules": ["Host-primary: host speech has priority."],
        "annotation_focus": "speaker_source",
        "label_options": ["a_translation_error", "b_stt_error", "both", "ok", "unclear"],
        "context_tag_options": ["clip_audio", "bgm_mixed", "over_attributed_chunks"],
        "speaker_source_options": ["host_only", "host_over_clip", "wrong_speaker_selected"],
        "sampling": {
            "method": "uniform_random_without_replacement",
            "seed": 123,
            "population_size": 150,
            "sample_size": 1,
        },
        "samples": [
            {
                "sample_id": "S001",
                "run_id": "run-a",
                "sequence_id": 1,
                "source_text": "source",
                "target_text": "target",
                "source_utterance_ids": ["utt-1"],
                "source_utterance_id_count": 1,
                "unique_source_utterance_id_count": 1,
                "source_chunks": [
                    {
                        "utterance_id": "utt-1",
                        "chunk_role": "primary",
                        "audio_path": str(audio_path),
                        "audio_exists": True,
                        "avg_logprob": -0.2,
                        "no_speech_prob": 0.01,
                    }
                ],
            }
        ],
    }


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_json(url):
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url, data):
    request = Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_default_annotation_path_keeps_sample_name(tmp_path):
    sample_path = tmp_path / "labeling_sample_20260531_161656.json"

    assert default_annotation_path(sample_path) == tmp_path / "labeling_sample_20260531_161656.annotations.json"


def test_romanize_korean_text_is_syllable_separated():
    assert romanize_korean_text("안녕하세요") == "an-nyeong-ha-se-yo"
    assert (
        romanize_korean_text("오마쿡스, 땡글즈, 띠빵뽕, 치이카와, Minecraft.")
        == "o-ma-kuk-seu, ttaeng-geul-jeu, tti-ppang-ppong, chi-i-ka-wa, Minecraft."
    )


def test_annotation_store_validates_and_persists(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF")
    sample_path = tmp_path / "sample.json"
    annotation_path = tmp_path / "sample.annotations.json"
    sample = _sample_data(audio_path)
    _write_json(sample_path, sample)

    store = AnnotationStore(path=annotation_path, sample_path=sample_path, sample_data=sample)
    result = store.update(
        {
            "sample_id": "S001",
            "label": "b_stt_error",
            "context_tags": ["bgm_mixed", "over_attributed_chunks"],
            "speaker_source_tags": ["host_over_clip", "wrong_speaker_selected"],
            "heard_source_text": "heard",
            "notes": "note",
        }
    )

    assert result["annotations"]["S001"]["label"] == "b_stt_error"
    assert result["annotations"]["S001"]["context_tags"] == ["bgm_mixed", "over_attributed_chunks"]
    assert result["annotations"]["S001"]["speaker_source_tags"] == ["host_over_clip", "wrong_speaker_selected"]
    saved = json.loads(annotation_path.read_text(encoding="utf-8"))
    assert saved["annotations"]["S001"]["heard_source_text"] == "heard"
    assert saved["annotations"]["S001"]["context_tags"] == ["bgm_mixed", "over_attributed_chunks"]
    assert saved["annotations"]["S001"]["speaker_source_tags"] == ["host_over_clip", "wrong_speaker_selected"]
    with pytest.raises(ValueError, match="unknown label"):
        store.update({"sample_id": "S001", "label": "not-a-label"})
    with pytest.raises(ValueError, match="unknown context tag"):
        store.update({"sample_id": "S001", "label": "ok", "context_tags": ["not-a-tag"]})
    with pytest.raises(ValueError, match="unknown speaker/source tag"):
        store.update({"sample_id": "S001", "label": "ok", "speaker_source_tags": ["not-a-speaker-tag"]})
    with pytest.raises(ValueError, match="unknown sample_id"):
        store.update({"sample_id": "S999", "label": "ok"})


def test_audio_map_adds_audio_urls_without_mutating_sample(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF")
    sample = _sample_data(audio_path)

    audio_map = build_audio_map(sample)
    public_samples = public_sample_data(sample, audio_map)

    assert audio_map == {"S001-1": audio_path}
    assert public_samples[0]["romanized_source_text"] == "source"
    assert public_samples[0]["source_chunks"][0]["audio_url"] == "/audio/S001-1.wav"
    assert "audio_url" not in sample["samples"][0]["source_chunks"][0]


def test_public_sample_data_orders_audio_by_stt_event_time(tmp_path):
    current_audio = tmp_path / "utt-2.wav"
    evidence_audio = tmp_path / "utt-1.wav"
    current_audio.write_bytes(b"RIFF-current")
    evidence_audio.write_bytes(b"RIFF-evidence")
    sample = _sample_data(current_audio)
    current_chunk = sample["samples"][0]["source_chunks"][0]
    current_chunk["utterance_id"] = "utt-2"
    current_chunk["stt_event_line"] = 20
    sample["samples"][0]["source_chunks"].append(
        {
            "utterance_id": "utt-1",
            "chunk_role": "prior_overlap",
            "source_kind": "evidence",
            "audio_path": str(evidence_audio),
            "audio_exists": True,
            "stt_event_line": 10,
        }
    )

    public_chunks = public_sample_data(sample, build_audio_map(sample))[0]["source_chunks"]

    assert [chunk["utterance_id"] for chunk in public_chunks] == ["utt-1", "utt-2"]
    assert [chunk["audio_url"] for chunk in public_chunks] == ["/audio/S001-2.wav", "/audio/S001-1.wav"]


def test_http_api_state_post_and_audio(tmp_path):
    audio_path = tmp_path / "utt-1.wav"
    audio_path.write_bytes(b"RIFF")
    sample_path = tmp_path / "sample.json"
    annotation_path = tmp_path / "sample.annotations.json"
    _write_json(sample_path, _sample_data(audio_path))

    server = LabelingHTTPServer(("127.0.0.1", 0), sample_path=sample_path, annotation_path=annotation_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        state = _read_json(f"{base}/api/state")
        assert state["annotation_focus"] == "speaker_source"
        assert state["samples"][0]["source_chunks"][0]["audio_url"] == "/audio/S001-1.wav"
        assert state["context_tag_options"] == ["clip_audio", "bgm_mixed", "over_attributed_chunks"]
        assert state["speaker_source_options"] == ["host_only", "host_over_clip", "wrong_speaker_selected"]
        assert state["speaker_policy"] == "host-primary"
        assert state["annotation_goal"] == "Judge host-primary subtitles."
        assert state["annotation_rules"] == ["Host-primary: host speech has priority."]

        result = _post_json(
            f"{base}/api/annotation",
            {
                "sample_id": "S001",
                "label": "ok",
                "context_tags": ["clip_audio"],
                "speaker_source_tags": ["host_only"],
                "heard_source_text": "",
                "notes": "done",
            },
        )
        assert result["annotations"]["S001"]["label"] == "ok"
        assert result["annotations"]["S001"]["context_tags"] == ["clip_audio"]
        assert result["annotations"]["S001"]["speaker_source_tags"] == ["host_only"]

        with urlopen(f"{base}/audio/S001-1.wav", timeout=5) as response:
            assert response.read() == b"RIFF"
        with pytest.raises(HTTPError) as exc:
            urlopen(f"{base}/audio/not-in-sample.wav", timeout=5)
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_load_sample_rejects_duplicate_ids(tmp_path):
    sample_path = tmp_path / "sample.json"
    sample = _sample_data(tmp_path / "utt-1.wav")
    sample["samples"].append(dict(sample["samples"][0]))
    _write_json(sample_path, sample)

    with pytest.raises(ValueError, match="duplicate sample_id"):
        load_sample(sample_path)
