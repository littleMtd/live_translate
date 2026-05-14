import json

from utils.runtime_events import RuntimeEventWriter, translation_quality


def test_runtime_event_writer_appends_jsonl(tmp_path):
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: "2026-05-14T00:00:00+00:00",
    )

    writer.emit("translation", source_text="안녕하세요", target_text="你好")

    files = list(tmp_path.glob("runtime_events_*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["event_type"] == "translation"
    assert record["run_id"] == "test-run"
    assert record["source_text"] == "안녕하세요"
    assert record["target_text"] == "你好"


def test_translation_quality_flags_low_hangul_source():
    result = translation_quality("I think this is fresh", "我覺得很新鮮")

    assert result["source_latin_ratio"] > 0.5
    assert "low_source_hangul" in result["quality_flags"]


def test_translation_quality_flags_empty_target():
    result = translation_quality("안녕하세요", None)

    assert result["target_len"] == 0
    assert "empty_target" in result["quality_flags"]
