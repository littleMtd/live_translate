import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.suggest_corrections import (
    build_hangul_allowlist,
    build_report,
    find_hangul_leaks,
    find_inconsistent_translations,
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
