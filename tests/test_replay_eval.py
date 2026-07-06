import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.replay_eval import build, evaluate_case, main, run  # noqa: E402


def _event(source, target, *, profile="hades_chxxnnx", status="success",
           filter_reason=""):
    return {
        "event_type": "translation",
        "source_text": source,
        "target_text": target,
        "status": status,
        "filter_reason": filter_reason,
        "profile_id": profile,
    }


def _write_events(path, events):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _build_snapshot(tmp_path):
    events_path = tmp_path / "runtime_events_20260701.jsonl"
    _write_events(events_path, [
        _event("챈나가 왔어요", "Chaenna來了"),
        _event("게임 시작합니다", "遊戲開始"),
        _event("사이트 들어가보세요", "", status="filtered",
               filter_reason="stt_garbage"),
        {"event_type": "translation", "engine": "mock",
         "source_text": "안녕하세요", "target_text": "你好",
         "status": "success", "profile_id": ""},  # must be excluded
    ])
    snapshot = tmp_path / "snapshot.jsonl"
    assert main(["build", "--events", str(events_path),
                 "--output", str(snapshot)]) == 0
    return snapshot


def test_build_excludes_mock_and_records_expectations(tmp_path):
    snapshot = _build_snapshot(tmp_path)
    cases = [json.loads(line) for line in open(snapshot, encoding="utf-8")]
    sources = [case["source"] for case in cases]
    assert "안녕하세요" not in sources  # mock engine excluded
    assert len(cases) == 3
    hades = next(c for c in cases if c["source"] == "챈나가 왔어요")
    assert hades["expect_target"] == "Chaenna來了"


def test_run_is_clean_right_after_build(tmp_path):
    snapshot = _build_snapshot(tmp_path)
    assert main(["run", "--snapshot", str(snapshot)]) == 0


def test_run_detects_divergence_and_update_accepts_it(tmp_path):
    snapshot = _build_snapshot(tmp_path)
    cases = [json.loads(line) for line in open(snapshot, encoding="utf-8")]
    for case in cases:
        if case["source"] == "챈나가 왔어요":
            case["expect_target"] = "Chxxnnx來了"  # pretend the old ruleset
    with open(snapshot, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    assert main(["run", "--snapshot", str(snapshot)]) == 1  # diff detected

    assert main(["run", "--snapshot", str(snapshot), "--update"]) == 0
    assert main(["run", "--snapshot", str(snapshot)]) == 0  # accepted


def test_evaluate_case_policy_layer_flags_filtered_source():
    assert evaluate_case("아", "", "hades_chxxnnx")["expect_rejection"] == "too_short"
    assert evaluate_case(
        "사이트 들어가보세요 광고 클릭", "", "hades_chxxnnx"
    )["expect_rejection"] == "stt_garbage"
