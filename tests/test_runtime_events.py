import json
import math
from datetime import timedelta, timezone

from utils.runtime_events import RuntimeEventWriter, _is_cjk, translation_quality


def test_runtime_event_writer_appends_jsonl(tmp_path):
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: "2026-05-14T00:00:00+00:00",
        filename_timezone=timezone.utc,
    )

    writer.emit("translation", source_text="안녕하세요", target_text="你好")

    files = list(tmp_path.glob("runtime_events_*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "runtime_events_20260514.jsonl"
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["event_type"] == "translation"
    assert record["run_id"] == "test-run"
    assert record["source_text"] == "안녕하세요"
    assert record["target_text"] == "你好"


def test_runtime_event_writer_filename_follows_injected_clock(tmp_path):
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: "2030-01-02T03:04:05+00:00",
        filename_timezone=timezone.utc,
    )

    writer.emit("translation", source_text="x")

    files = sorted(p.name for p in tmp_path.glob("runtime_events_*.jsonl"))
    assert files == ["runtime_events_20300102.jsonl"]


def test_runtime_event_writer_filename_can_use_local_date(tmp_path):
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: "2026-05-18T16:33:55+00:00",
        filename_timezone=timezone(timedelta(hours=8)),
    )

    writer.emit("translation", source_text="x")

    files = sorted(p.name for p in tmp_path.glob("runtime_events_*.jsonl"))
    assert files == ["runtime_events_20260519.jsonl"]


def test_runtime_event_writer_normalizes_nonjson_values(tmp_path):
    class _Custom:
        def __repr__(self) -> str:
            return "<custom obj>"

    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="run",
        clock=lambda: "2026-05-14T00:00:00+00:00",
        filename_timezone=timezone.utc,
    )

    writer.emit(
        "translation",
        nan_value=float("nan"),
        inf_value=float("inf"),
        nested={"items": (1, 2, _Custom())},
        custom=_Custom(),
    )

    record = json.loads((tmp_path / "runtime_events_20260514.jsonl").read_text(encoding="utf-8"))
    assert record["nan_value"] is None
    assert record["inf_value"] is None
    assert record["nested"] == {"items": [1, 2, "<custom obj>"]}
    assert record["custom"] == "<custom obj>"


def test_runtime_event_writer_handles_numpy_scalar(tmp_path):
    try:
        import numpy as np  # type: ignore
    except ImportError:
        import pytest

        pytest.skip("numpy not installed")

    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="run",
        clock=lambda: "2026-05-14T00:00:00+00:00",
        filename_timezone=timezone.utc,
    )

    writer.emit("translation", avg_logprob=np.float64(-0.25))

    record = json.loads((tmp_path / "runtime_events_20260514.jsonl").read_text(encoding="utf-8"))
    assert isinstance(record["avg_logprob"], float)
    assert math.isclose(record["avg_logprob"], -0.25, abs_tol=1e-9)


def test_translation_quality_flags_low_hangul_source():
    result = translation_quality("I think this is fresh", "我覺得很新鮮")

    assert result["source_latin_ratio"] > 0.5
    assert "low_source_hangul" in result["quality_flags"]


def test_translation_quality_flags_empty_target():
    result = translation_quality("안녕하세요", None)

    assert result["target_len"] == 0
    assert "empty_target" in result["quality_flags"]


def test_translation_quality_observes_target_scripts():
    result = translation_quality("시라유키 히나 해둥이", "這是 Shirayuki Hina ちゃん 해둥이")

    assert result["target_hangul_ratio"] > 0
    assert result["target_latin_ratio"] > 0
    assert result["target_japanese_count"] > 0
    assert "target_has_hangul" in result["quality_flags"]
    assert "target_high_latin" in result["quality_flags"]
    assert "target_has_japanese" in result["quality_flags"]


def test_is_cjk_covers_extension_ranges():
    # Basic CJK
    assert _is_cjk("中")
    # Extension A (U+3400-U+4DBF) — pick an arbitrary character in range
    assert _is_cjk(chr(0x3400))
    assert _is_cjk(chr(0x4DBF))
    # Compatibility Ideographs (U+F900-U+FAFF)
    assert _is_cjk(chr(0xF900))
    # Extension B (U+20000+) — uses an SMP code point
    assert _is_cjk(chr(0x20000))
    # Non-CJK characters
    assert not _is_cjk("A")
    assert not _is_cjk("가")
    assert not _is_cjk(" ")
