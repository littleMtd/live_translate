import json
import wave

from scripts.gemini_live_translate_probe import build_audio_payload, main, select_samples


def _write_wav(path, *, seconds=0.1, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b"\x00\x00" * int(rate * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)


def _sample(sample_id, bucket, audio_paths):
    return {
        "sample_id": sample_id,
        "phase0_bucket": bucket,
        "run_id": "run-a",
        "translation_event_id": f"evt-{sample_id}",
        "translation_index": 1,
        "translation_cut_reason": "natural",
        "source_utterance_ids": [f"utt-{sample_id}"],
        "evidence_source_utterance_ids": [],
        "source_chunk_usages": [],
        "source_text": "안녕하세요",
        "target_text": "你好",
        "quality_flags": [],
        "source_chunks": [
            {
                "utterance_id": f"utt-{sample_id}-{index}",
                "chunk_role": "primary",
                "audio_path": str(path),
            }
            for index, path in enumerate(audio_paths, start=1)
        ],
    }


def test_select_samples_can_filter_by_bucket_and_limit(tmp_path):
    pool = {
        "samples": [
            _sample("S001", "forced_cut", [tmp_path / "a.wav"]),
            _sample("S002", "random_holdout", [tmp_path / "b.wav"]),
            _sample("S003", "forced_cut", [tmp_path / "c.wav"]),
        ]
    }

    selected = select_samples(pool, limit=1, buckets={"forced_cut"})

    assert [sample["sample_id"] for sample in selected] == ["S001"]


def test_build_audio_payload_concatenates_source_chunks_with_silence(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _write_wav(first, seconds=0.1)
    _write_wav(second, seconds=0.2)
    sample = _sample("S001", "forced_cut", [first, second])

    payload = build_audio_payload(sample, inter_chunk_silence_ms=100, max_audio_seconds=None)

    assert payload["total_seconds"] == 0.4
    assert payload["truncated"] is False
    assert len(payload["chunks"]) == 2
    assert len(payload["pcm"]) == int(16000 * 2 * 0.4)


def test_main_dry_run_writes_probe_jsonl(tmp_path):
    audio = tmp_path / "audio.wav"
    _write_wav(audio, seconds=0.1)
    pool = {
        "speaker_policy": "host-primary",
        "sampling": {"method": "phase0_stratified_candidates"},
        "samples": [_sample("S001", "forced_cut", [audio])],
    }
    sample_file = tmp_path / "sample.json"
    output = tmp_path / "probe.jsonl"
    sample_file.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")

    exit_code = main(
        [
            "--sample-file",
            str(sample_file),
            "--output",
            str(output),
            "--dry-run",
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["status"] == "dry_run"
    assert rows[0]["dry_run"] is True
    assert rows[0]["sample"]["sample_id"] == "S001"
    assert rows[0]["audio"]["total_seconds"] == 0.1
