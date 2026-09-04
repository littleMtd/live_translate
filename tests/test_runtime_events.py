import json
import math
from datetime import timedelta, timezone

from utils.runtime_events import (
    RuntimeEventWriter,
    _default_run_id,
    _is_cjk,
    quality_score,
    quality_severity,
    source_proven_quality_terms,
    translation_quality,
)


def test_default_run_id_honors_safe_launcher_override(monkeypatch):
    monkeypatch.setenv("LIVE_TRANSLATE_RUN_ID", "dashboard-123-4")
    assert _default_run_id() == "dashboard-123-4"


def test_default_run_id_rejects_control_character_override(monkeypatch):
    monkeypatch.setenv("LIVE_TRANSLATE_RUN_ID", "bad\nrun")
    assert _default_run_id() != "bad\nrun"


def test_runtime_event_writer_appends_jsonl(tmp_path):
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: "2026-05-14T00:00:00+00:00",
        filename_timezone=timezone.utc,
        run_kind="benchmark",
        git_sha="abc123",
        git_dirty=True,
    )

    writer.emit("translation", source_text="안녕하세요", target_text="你好")

    files = list(tmp_path.glob("runtime_events_*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "runtime_events_20260514.jsonl"
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == 5
    assert record["event_type"] == "translation"
    assert record["run_id"] == "test-run"
    assert record["run_kind"] == "benchmark"
    assert record["git_sha"] == "abc123"
    assert record["git_dirty"] is True
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


def test_runtime_event_writer_provenance_fields_cannot_be_overridden(tmp_path):
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: "2026-05-14T00:00:00+00:00",
        filename_timezone=timezone.utc,
        run_kind="test",
        git_sha="trusted",
        git_dirty=False,
    )

    writer.emit(
        "translation",
        run_kind="live",
        git_sha="spoofed",
        git_dirty=True,
    )

    record = json.loads((tmp_path / "runtime_events_20260514.jsonl").read_text(encoding="utf-8"))
    assert record["run_kind"] == "test"
    assert record["git_sha"] == "trusted"
    assert record["git_dirty"] is False


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


def test_runtime_event_filename_uses_same_timestamp_as_record(tmp_path):
    timestamps = iter(
        [
            "2026-05-14T23:59:59.999999+00:00",
            "2026-05-15T00:00:00+00:00",
        ]
    )
    writer = RuntimeEventWriter(
        log_dir=tmp_path,
        run_id="test-run",
        clock=lambda: next(timestamps),
        filename_timezone=timezone.utc,
    )

    writer.emit("translation", source_text="x")

    path = tmp_path / "runtime_events_20260514.jsonl"
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["created_at"] == "2026-05-14T23:59:59.999999+00:00"


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
    assert result["target_unexpected_hangul_spans"] == ["해둥이"]
    assert "target_has_unexpected_hangul" in result["quality_classifications"]


def test_translation_quality_separates_profile_approved_hangul_and_latin():
    result = translation_quality(
        "히나가 해둥이와 새 노래를 소개했어요",
        "Hina介紹了해둥이和Wish Me Love。",
        approved_terms={"Hina", "해둥이", "Wish Me Love"},
    )

    assert result["target_approved_hangul_spans"] == ["해둥이"]
    assert result["target_unexpected_hangul_spans"] == []
    assert result["target_approved_latin_spans"] == ["Hina", "Wish", "Me", "Love"]
    assert result["target_unexpected_latin_spans"] == []
    assert "target_has_approved_hangul" in result["quality_classifications"]
    assert "target_high_latin_approved_only" in result["quality_classifications"]
    # T04 is telemetry-only: legacy flags/scores stay unchanged.
    assert "target_has_hangul" in result["quality_flags"]
    assert "target_high_latin" in result["quality_flags"]


def test_translation_quality_recognizes_spotify_ktv_and_source_id():
    result = translation_quality(
        "viewer_42가 노래방과 음악 앱 얘기를 했어요",
        "viewer_42說等等去KTV，現在用Spotify聽歌。",
    )

    assert result["target_approved_latin_spans"] == ["viewer_42", "KTV", "Spotify"]
    assert result["target_unexpected_latin_spans"] == []
    assert "target_high_latin_approved_only" in result["quality_classifications"]


def test_translation_quality_excludes_ascii_sentence_punctuation_from_latin_spans():
    result = translation_quality(
        "viewer_42. Wish Me Love와 음악 앱 얘기를 했어요",
        "viewer_42. Spotify. Wish Me Love.",
        approved_terms={"Wish Me Love"},
    )

    assert result["target_approved_latin_spans"] == [
        "viewer_42",
        "Spotify",
        "Wish",
        "Me",
        "Love",
    ]
    assert result["target_unexpected_latin_spans"] == []
    assert "target_high_latin_approved_only" in result["quality_classifications"]
    assert "target_high_latin_unexpected" not in result["quality_classifications"]


def test_translation_quality_does_not_approve_arbitrary_uppercase_words():
    for source in ("HELLO WORLD", "THIS IS BAD"):
        result = translation_quality(source, source)

        assert result["target_approved_latin_spans"] == []
        assert result["target_unexpected_latin_spans"]
        assert "target_high_latin_unexpected" in result["quality_classifications"]
        assert "target_high_latin_approved_only" not in result["quality_classifications"]


def test_translation_quality_approves_only_source_proven_pvp_rp_acronyms():
    for source, target in (
        ("PVP에서 이겼어요", "PVP模式獲勝了。"),
        ("RP를 시작할게요", "現在開始RP模式。"),
    ):
        result = translation_quality(
            source,
            target,
            approved_terms=source_proven_quality_terms(source),
        )
        assert result["target_unexpected_latin_spans"] == []
        assert "target_high_latin_approved_only" in result["quality_classifications"]

    for target in ("現在開始PVP競技模式。", "現在開始進行RP角色扮演。"):
        result = translation_quality("게임을 시작할게요", target)
        assert result["target_unexpected_latin_spans"]
        assert result["target_approved_latin_spans"] == []


def test_translation_quality_recognizes_structured_source_terms():
    for source, preserved in (
        ("https://example.com/watch?v=42", "https://example.com/watch?v=42"),
        ("user@example.com", "user@example.com"),
        ("www.example.com", "www.example.com"),
        ("https://example.com에서", "https://example.com"),
        ("user@example.com으로", "user@example.com"),
        ("www.example.com에서", "www.example.com"),
    ):
        result = translation_quality(source, preserved)

        assert result["target_approved_latin_spans"]
        assert result["target_unexpected_latin_spans"] == []
        assert "target_high_latin_approved_only" in result["quality_classifications"]
        assert "target_high_latin_unexpected" not in result["quality_classifications"]


def test_translation_quality_keeps_unapproved_latin_visible():
    result = translation_quality(
        "음악 앱이 이상하대요",
        "Spotify is suddenly unavailable right now。",
    )

    assert "Spotify" in result["target_approved_latin_spans"]
    assert result["target_unexpected_latin_spans"] == [
        "is",
        "suddenly",
        "unavailable",
        "right",
        "now",
    ]
    assert "target_high_latin_unexpected" in result["quality_classifications"]


def test_translation_quality_target_high_latin_is_diagnostic_only():
    result = translation_quality(
        "\uc624\ub298 \uacf5\uac1c\ub41c \uc0c8\ub85c\uc6b4 \ub178\ub798\uc640 \uc804\uccb4 "
        "\uc774\ubca4\ud2b8 \uc774\ub984\uc744 \ud558\ub098\uc529 \uc790\uc138\ud788 "
        "\uc18c\uac1c\ud558\uace0 \uc788\uc2b5\ub2c8\ub2e4",
        "\u4eca\u5929\u516c\u958b\u7684\u65b0\u6b4c\u662f Smile For You\uff0c"
        "\u53e6\u5916\u9084\u63d0\u5230 JMT \u9019\u500b\u540d\u7a31\uff0c"
        "\u9019\u4e9b\u5c08\u6709\u540d\u8a5e\u4fdd\u7559\u82f1\u6587\u3002",
    )

    assert "target_high_latin" in result["quality_flags"]
    assert result["quality_score"] == 1.0
    assert result["quality_severity"] == "ok"


def test_translation_quality_clean_output_scores_high():
    result = translation_quality("안녕하세요 오늘 날씨 좋네요", "你好，今天天氣真好")

    assert result["quality_flags"] == []
    assert result["quality_score"] == 1.0
    assert result["quality_severity"] == "ok"


def test_translation_quality_flags_repetitive_target():
    result = translation_quality("안녕하세요 반갑습니다", "好好好好好好好好")

    assert "repetitive_target" in result["quality_flags"]
    assert result["target_distinct_bigram_ratio"] < 0.5
    assert result["quality_severity"] in ("warn", "bad")


def test_translation_quality_flags_meta_leak():
    result = translation_quality("안녕하세요", "translation: 你好")

    assert "target_meta_leak" in result["quality_flags"]
    # Single 0.6 penalty -> 0.4 -> bad.
    assert result["quality_severity"] == "bad"


def test_translation_quality_flags_english_refusal_leak():
    result = translation_quality("안녕하세요", "I'm sorry, I cannot translate this")

    assert "target_meta_leak" in result["quality_flags"]


def test_translation_quality_apology_translation_is_not_meta_leak():
    # A genuine apology rendered in zh-TW must NOT be mistaken for a refusal.
    result = translation_quality("미안해요 진짜", "抱歉啦真的")

    assert "target_meta_leak" not in result["quality_flags"]


def test_translation_quality_flags_unbalanced_brackets():
    result = translation_quality("안녕(반가워", "你好（很高興")

    assert "unbalanced_brackets" in result["quality_flags"]


def test_translation_quality_flags_amount_mismatch_for_lost_man_unit():
    result = translation_quality("만 5천원 받았어", "收到五千")

    assert result["source_amount_values"] == [15000]
    assert result["target_amount_values"] == [5000]
    assert result["amount_mismatch_candidate"] is True
    assert "amount_mismatch" not in result["quality_flags"]
    assert result["quality_severity"] == "ok"


def test_translation_quality_accepts_matching_mixed_amount_units():
    result = translation_quality("만 5천원 받았어", "收到一萬五千元")

    assert result["source_amount_values"] == [15000]
    assert result["target_amount_values"] == [15000]
    assert result["amount_mismatch_candidate"] is False
    assert "amount_mismatch" not in result["quality_flags"]


def test_translation_quality_does_not_treat_months_as_amounts():
    result = translation_quality("10월에 한다네", "10月舉行")

    assert result["source_amount_values"] == []
    assert result["target_amount_values"] == []
    assert result["amount_mismatch_candidate"] is False
    assert "amount_mismatch" not in result["quality_flags"]


def test_translation_quality_does_not_treat_won_words_as_amounts():
    for source in ("원래 그랬어", "원인 몰라", "구원 투수 왔어"):
        result = translation_quality(source, "這不是金額")

        assert result["source_amount_values"] == []
        assert result["amount_mismatch_candidate"] is False


def test_translation_quality_does_not_merge_subject_particle_into_amount():
    result = translation_quality("이 천원은 너무 싸다", "這一千元太便宜")

    assert result["source_amount_values"] == [1000]
    assert result["target_amount_values"] == [1000]
    assert result["amount_mismatch_candidate"] is False


def test_translation_quality_repeated_same_amount_can_be_translated_once():
    result = translation_quality("만원 만원 만원 받았어", "收到一萬元")

    assert result["source_amount_values"] == [10000, 10000, 10000]
    assert result["target_amount_values"] == [10000]
    assert result["amount_mismatch_candidate"] is False


def test_translation_quality_price_range_does_not_concatenate_digits():
    result = translation_quality("2, 3만원 정도야", "大概兩三萬元")

    assert 230000 not in result["source_amount_values"]
    assert result["amount_mismatch_candidate"] is False


def test_translation_quality_empty_target_scores_zero():
    result = translation_quality("안녕하세요", None)

    assert result["quality_score"] == 0.0
    assert result["quality_severity"] == "bad"


def test_quality_score_is_monotonic_in_penalties():
    clean = quality_score([])
    one_flag = quality_score(["target_has_hangul"])
    two_flags = quality_score(["target_has_hangul", "low_target_cjk"])

    assert clean == 1.0
    assert clean > one_flag > two_flags
    assert two_flags >= 0.0


def test_quality_severity_thresholds():
    assert quality_severity(1.0) == "ok"
    assert quality_severity(0.8) == "ok"
    assert quality_severity(0.79) == "warn"
    assert quality_severity(0.5) == "warn"
    assert quality_severity(0.49) == "bad"


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
