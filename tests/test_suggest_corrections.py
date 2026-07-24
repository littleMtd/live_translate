import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import suggest_corrections as suggestions
from scripts.suggest_corrections import (
    build_candidate_data,
    build_hangul_allowlist,
    build_report,
    find_frequent_corrections,
    find_hangul_leaks,
    find_inconsistent_translations,
    find_profile_term_misses,
    iter_translation_events,
)


def _event(source, target, *, status="success", profile="stellive_hina",
           severity="ok", run_id="run-1"):
    return {
        "event_type": "translation",
        "run_id": run_id,
        "source_text": source,
        "target_text": target,
        "status": status,
        "quality_severity": severity,
        "profile_id": profile,
    }


def _write_jsonl(path, events):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


ALLOWLIST, SUFFIXES = build_hangul_allowlist()


def test_allowlist_contains_intentional_keep_terms():
    # Fan names that the profiles deliberately keep in zh-TW output.
    assert "해둥이" in ALLOWLIST
    assert "띵띵이" in ALLOWLIST


def test_leak_detection_flags_unknown_hangul():
    events = [
        _event("아 연말 마크 유입 그럼 시둥이", "啊，年末Minecraft進來，那시둥이？"),
        _event("시둥이 어디 갔어", "시둥이去哪了？"),
    ]
    leaks = find_hangul_leaks(events, ALLOWLIST, SUFFIXES)
    tokens = [c.token for c in leaks]
    assert "시둥이" in tokens
    leak = next(c for c in leaks if c.token == "시둥이")
    assert leak.count == 2
    assert leak.profiles["stellive_hina"] == 2


def test_leak_detection_allows_keep_terms_and_suffixed_forms():
    events = [
        _event("해둥이들 안녕", "해둥이們好！"),
        _event("해둥이들 안녕", "해둥이들好！"),  # base + known suffix
    ]
    leaks = find_hangul_leaks(events, ALLOWLIST, SUFFIXES)
    assert leaks == []


def test_leak_detection_skips_failed_events():
    events = [_event("뭐", "시둥이", status="failed")]
    assert find_hangul_leaks(events, ALLOWLIST, SUFFIXES) == []


def test_unknown_hangul_prefers_profile_aware_runtime_classification():
    classified = _event("모카가 왔어", "모카來了")
    classified["target_unexpected_hangul_spans"] = []
    unexpected = _event("시둥이 왔어", "시둥이來了")
    unexpected["target_unexpected_hangul_spans"] = ["시둥이", "시둥이"]

    candidates = find_hangul_leaks(
        [classified, unexpected],
        ALLOWLIST,
        SUFFIXES,
    )

    assert [candidate.token for candidate in candidates] == ["시둥이"]
    candidate = candidates[0]
    assert candidate.count == 1
    assert candidate.source_presence_count == 1
    assert candidate.telemetry_event_count == 1
    assert candidate.fallback_event_count == 0


def test_unknown_hangul_uses_legacy_fallback_only_when_field_absent():
    legacy = _event("시둥이 왔어", "시둥이來了")
    candidates = find_hangul_leaks([legacy], ALLOWLIST, SUFFIXES)
    assert candidates[0].telemetry_event_count == 0
    assert candidates[0].fallback_event_count == 1


def test_unknown_hangul_uses_effective_profile_attribution():
    event = _event("시둥이 왔어", "시둥이來了", profile="url")
    event["profile_applied"] = False
    event["target_unexpected_hangul_spans"] = ["시둥이"]

    candidates = find_hangul_leaks([event], ALLOWLIST, SUFFIXES)

    assert candidates[0].profiles == {"": 1}


def test_inconsistent_translations_detected():
    events = [
        _event("게임 시작합니다", "遊戲開始"),
        _event("게임 시작합니다", "開始遊戲"),
        _event("게임 시작합니다", "遊戲開始"),
        _event("안녕하세요", "大家好"),
    ]
    results = find_inconsistent_translations(events)
    assert len(results) == 1
    assert results[0].source == "게임 시작합니다"
    assert results[0].total == 3
    assert results[0].variants["遊戲開始"] == 2


def test_inconsistent_translations_do_not_cross_profile_boundaries():
    events = [
        _event("같은 문장", "第一種", profile="profile-a"),
        _event("같은 문장", "第二種", profile="profile-b"),
    ]
    assert find_inconsistent_translations(events) == []


def test_inconsistent_translations_separate_explicitly_disabled_profile():
    disabled = _event("같은 문장", "一般譯法", profile="url")
    disabled["profile_applied"] = False
    applied = _event("같은 문장", "URL詞條譯法", profile="url")
    applied["profile_applied"] = True

    assert find_inconsistent_translations([disabled, applied]) == []


def test_profile_term_misses_are_exact_and_profile_scoped():
    terms = [
        {
            "profile_id": "hades_chxxnnx",
            "term": "챈나미",
            "rendering": "Chaenna粉",
            "aliases": ["찬나미"],
        }
    ]
    events = [
        _event(
            "찬나미들이 왔어",
            "챈나미們來了",
            profile="hades_chxxnnx",
        ),
        _event(
            "챈나미들이 왔어",
            "Chaenna粉來了",
            profile="hades_chxxnnx",
        ),
        _event("찬나미들이 왔어", "챈나미們來了", profile="url"),
        _event(
            "찬나미들이 왔어",
            "챈나미們來了",
            profile="hades_chxxnnx",
            status="failed",
        ),
    ]

    candidates = find_profile_term_misses(events, terms)

    assert len(candidates) == 1
    assert candidates[0].profile_id == "hades_chxxnnx"
    assert candidates[0].term == "챈나미"
    assert candidates[0].rendering == "Chaenna粉"
    assert candidates[0].count == 1
    assert candidates[0].matched_source_forms["찬나미"] == 1


def test_profile_term_misses_skip_explicitly_disabled_profile():
    terms = [{
        "profile_id": "url",
        "term": "유아렐",
        "rendering": "UR:L",
        "aliases": ["유아엘"],
    }]
    event = _event("유아엘 얘기야", "URL的話題", profile="url")
    event["profile_applied"] = False

    assert find_profile_term_misses([event], terms) == []


def test_profile_term_misses_exclude_declared_ambiguous_aliases():
    terms = [{
        "profile_id": "url",
        "term": "유아렐",
        "rendering": "UR:L",
        "aliases": ["UR:L", "URL", "유아엘"],
        "ambiguous_aliases": ["URL"],
    }]
    ambiguous = _event("URL 로아 전파요?", "要分享遊戲網址嗎？", profile="url")
    strong = _event("유아엘 멤버가 왔어", "團體成員來了", profile="url")

    candidates = find_profile_term_misses([ambiguous, strong], terms)

    assert len(candidates) == 1
    assert candidates[0].count == 1
    assert candidates[0].matched_source_forms == {"유아엘": 1}


def test_frequent_corrections_count_events_and_ignore_malformed_duplicates():
    first = _event("랑코가 왔어", "랑코來了", profile="url")
    correction = {
        "stage": "name_render",
        "rule": "name:랑코",
        "before": "蘭科",
        "after": "랑코",
    }
    first["corrections"] = [correction, correction, "bad"]
    second = _event("랑코야", "랑코啊", profile="url")
    second["corrections"] = [correction]

    candidates = find_frequent_corrections([first, second])

    assert len(candidates) == 1
    assert candidates[0].count == 2
    assert candidates[0].profiles["url"] == 2
    assert len(candidates[0].examples) == 2


def test_iter_translation_events_filters_and_tolerates_garbage(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = [
        json.dumps(_event("a", "b", run_id="run-1"), ensure_ascii=False),
        "not json at all",
        json.dumps({"event_type": "stt", "run_id": "run-1"}),
        json.dumps(_event("c", "d", run_id="run-2"), ensure_ascii=False),
        "",
    ]
    path.write_text("\n".join(rows), encoding="utf-8")

    all_events = list(iter_translation_events([str(path)]))
    assert len(all_events) == 2

    filtered = list(iter_translation_events([str(path)], run_ids={"run-2"}))
    assert len(filtered) == 1
    assert filtered[0]["source_text"] == "c"


def test_iter_translation_events_deduplicates_overlapping_paths(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [_event("a", "b")])

    events = list(iter_translation_events([str(path), str(path)]))

    assert len(events) == 1


def test_build_report_end_to_end(tmp_path):
    events = [
        _event("아 시둥이 왔다", "啊，시둥이來了", severity="warn"),
        _event("아 시둥이 왔다", "啊，시둥이來了", severity="ok"),
        _event("게임 시작합니다", "遊戲開始"),
        _event("게임 시작합니다", "開始遊戲"),
    ]
    report = build_report(events, min_count=2)
    assert "시둥이" in report
    assert "게임 시작합니다" in report
    assert "warn: 1" in report


def test_candidate_data_is_machine_readable_and_candidate_only():
    event = _event("시둥이 왔어", "시둥이來了")
    event["target_unexpected_hangul_spans"] = ["시둥이"]
    event["corrections"] = [
        {
            "stage": "source_norm",
            "rule": "시둥->시둥이",
            "before": "시둥",
            "after": "시둥이",
        }
    ]

    data = build_candidate_data([event], min_count=1)
    encoded = json.dumps(data, ensure_ascii=False)

    assert data["schema_version"] == 1
    assert data["guardrails"] == {
        "candidate_only": True,
        "mutates_glossary": False,
        "requires_manual_labels": False,
    }
    assert data["candidates"]["unknown_hangul"][0]["token"] == "시둥이"
    assert data["candidates"]["frequent_corrections"][0]["count"] == 1
    assert "시둥이" in encoded


def test_candidate_data_rejects_invalid_min_count():
    try:
        build_candidate_data([], min_count=0)
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("expected invalid min_count to fail")


def test_cli_writes_parseable_json_artifact(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    markdown_path = tmp_path / "suggestions.md"
    json_path = tmp_path / "glossary_candidates.json"
    event = _event("시둥이 왔어", "시둥이來了")
    event["target_unexpected_hangul_spans"] = ["시둥이"]
    _write_jsonl(events_path, [event])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "suggest_corrections.py",
            "--events",
            str(events_path),
            "--min-count",
            "1",
            "--output",
            str(markdown_path),
            "--json-output",
            str(json_path),
        ],
    )

    assert suggestions.main() == 0
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Automatic Glossary Candidate Report"
    )
    artifact = json.loads(json_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert artifact["candidates"]["unknown_hangul"][0]["count"] == 1
