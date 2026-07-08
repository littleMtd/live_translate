import json
from pathlib import Path

from modules.streamer_profiles import known_profile_ids
from scripts import llm_quality_reviewer as reviewer


def _event(
    source: str,
    target: str,
    *,
    index: int = 1,
    status: str = "success",
    severity: str = "ok",
    flags: list[str] | None = None,
    **extra,
):
    return {
        "event_type": "translation",
        "created_at": f"2026-07-07T12:00:{index:02d}+00:00",
        "run_id": "run-1",
        "sequence_id": index,
        "source_text": source,
        "target_text": target,
        "status": status,
        "quality_severity": severity,
        "quality_flags": flags or [],
        "profile_id": "hades_chxxnnx",
        "current_activity": "Hades",
        "engine": "groq",
        **extra,
    }


def _write_jsonl(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_iter_translation_events_filters_run_id_and_garbage(tmp_path):
    path = tmp_path / "runtime_events.jsonl"
    rows = [
        _event("안녕", "你好", index=1),
        {"event_type": "stt", "run_id": "run-1"},
        _event("다른 런", "別的 run", index=2, run_id="run-2"),
    ]
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(rows[0], ensure_ascii=False) + "\n")
        handle.write("not json\n")
        handle.write(json.dumps(rows[1], ensure_ascii=False) + "\n")
        handle.write(json.dumps(rows[2], ensure_ascii=False) + "\n")

    events = list(reviewer.iter_translation_events([path], run_ids={"run-2"}))

    assert len(events) == 1
    assert events[0]["source_text"] == "다른 런"
    assert events[0]["_line_number"] == 4


def test_suspicion_score_prioritizes_known_runtime_failure_shapes():
    event = _event(
        "찬나미들 천재야",
        "我們Chaenna們才是天才",
        amount_mismatch_candidate=True,
        source_amount_values=[15000],
        target_amount_values=[5000],
    )

    score, reasons = reviewer.suspicion_score(event)

    assert score >= 10
    assert "amount_mismatch_candidate" in reasons
    assert "fan_name_plural_shape" in reasons


def test_suspicion_score_does_not_treat_allowed_hangul_as_leak():
    event = _event(
        "해둥이는 언제가 더 편해요?",
        "해둥이哪天比較方便？",
        severity="warn",
        flags=["target_has_hangul"],
    )

    score, reasons = reviewer.suspicion_score(
        event,
        allowlist=frozenset({"해둥이"}),
        suffixes=frozenset(),
    )

    assert score == 3
    assert not any(reason.startswith("target_contains_unallowed_hangul") for reason in reasons)


def test_suspicion_score_flags_missing_fixed_fan_term_rendering():
    event = _event(
        "랑코랑 모카가 같이 왔어",
        "蘭子和摩卡一起來了",
        profile_id="url",
    )

    score, reasons = reviewer.suspicion_score(
        event,
        fan_terms=reviewer.load_fan_terms(),
    )

    assert score >= 8
    assert any(
        reason.startswith("fixed_term_rendering_missing:")
        and "랑코->랑코" in reason
        and "모카->모카" in reason
        for reason in reasons
    )


def test_load_fan_terms_contains_group_ownership_metadata():
    fan_terms = reviewer.load_fan_terms()
    haedungi = next(term for term in fan_terms if term["term"] == "해둥이")

    assert haedungi["profile_id"] == "stellive_hina"
    assert haedungi["group"] == "Stellive"
    assert haedungi["fandom_of"] == "Shirayuki Hina fans"
    assert "해동이" in haedungi["aliases"]


def test_load_fan_terms_covers_all_streamer_profiles():
    profiles = known_profile_ids() - {""}
    covered = {term["profile_id"] for term in reviewer.load_fan_terms()}

    assert profiles <= covered


def test_load_fan_terms_contains_url_group_and_member_terms():
    fan_terms = reviewer.load_fan_terms()
    url_terms = {
        term["term"]: term
        for term in fan_terms
        if term["profile_id"] == "url"
    }

    assert {"유아렐", "모카", "랑코", "마냥", "솜먕"} <= set(url_terms)
    assert url_terms["유아렐"]["rendering"] == "UR:L"
    assert "유아엘" in url_terms["유아렐"]["aliases"]
    assert "YOU ARE LINKED" in url_terms["유아렐"]["aliases"]
    assert "결속아이돌" in url_terms["유아렐"]["aliases"]
    assert url_terms["랑코"]["group"] == "UR:L"
    assert url_terms["샌드박스 네트워크"]["rendering"] == "Sandbox Network"
    assert url_terms["플럭서스"]["rendering"] == "Fluxus"


def test_select_review_cases_uses_compact_translation_cases_with_context():
    events = [
        _event("앞 문장", "前一句", index=1),
        _event(
            "만 5천원 받았어",
            "收到五千",
            index=2,
            amount_mismatch_candidate=True,
            source_amount_values=[15000],
            target_amount_values=[5000],
        ),
        _event("뒤 문장", "後一句", index=3),
    ]

    cases = reviewer.select_review_cases(
        events,
        mode="suspicious",
        max_cases=1,
        control_cases=0,
        context_window=1,
    )

    assert len(cases) == 1
    assert cases[0].source_text == "만 5천원 받았어"
    assert cases[0].context_before[0]["source"] == "앞 문장"
    assert cases[0].context_after[0]["source"] == "뒤 문장"
    prompt_case = cases[0].to_prompt_dict()
    assert "source" in prompt_case
    assert "translation" in prompt_case
    assert "event_type" not in prompt_case


def test_select_review_cases_uses_fan_terms_to_find_name_mistranslations():
    cases = reviewer.select_review_cases(
        [
            _event("앞 문장", "前一句", index=1, profile_id="url"),
            _event("랑코랑 모카가 같이 왔어", "蘭子和摩卡一起來了", index=2, profile_id="url"),
        ],
        max_cases=1,
        control_cases=0,
    )

    assert len(cases) == 1
    assert cases[0].source_text == "랑코랑 모카가 같이 왔어"
    assert any(
        reason.startswith("fixed_term_rendering_missing:")
        for reason in cases[0].suspicion_reasons
    )


def test_review_case_includes_matched_fan_terms():
    cases = reviewer.select_review_cases(
        [_event("해둥이는 언제가 더 편해요?", "해둥이哪天比較方便？", profile_id="stellive_hina")],
        mode="broad",
        max_cases=1,
    )

    matched = cases[0].to_prompt_dict()["metadata"]["matched_fan_terms"]

    assert matched[0]["term"] == "해둥이"
    assert matched[0]["group"] == "Stellive"


def test_review_case_matches_url_member_terms():
    cases = reviewer.select_review_cases(
        [_event("랑코랑 모카가 같이 왔어", "蘭子和摩卡一起來了", profile_id="url")],
        mode="broad",
        max_cases=1,
    )

    matched = cases[0].to_prompt_dict()["metadata"]["matched_fan_terms"]
    terms = {term["term"] for term in matched}

    assert {"랑코", "모카"} <= terms
    assert all(term["group"] == "UR:L" for term in matched)


def test_build_messages_includes_live_stream_platform_background():
    case = reviewer.select_review_cases(
        [_event("CHZZK에서 방송했어", "在CHZZK直播了", index=1)],
        mode="broad",
        max_cases=1,
    )[0]

    messages = reviewer.build_messages([case])
    system = messages[0]["content"]

    assert "low-latency Korean live-stream subtitle" in system
    assert "SOOP" in system
    assert "CHZZK" in system
    assert "platform slang" in system
    assert "not to make every subtitle prettier" in system


def test_build_messages_includes_known_fan_terms_field():
    case = reviewer.select_review_cases(
        [_event("해둥이는 언제가 더 편해요?", "해둥이哪天比較方便？", profile_id="stellive_hina")],
        mode="broad",
        max_cases=1,
    )[0]

    messages = reviewer.build_messages([case])
    payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])

    assert "known_fan_terms" in payload["project_context"]
    assert any(term["term"] == "해둥이" for term in payload["project_context"]["known_fan_terms"])
    assert payload["cases"][0]["metadata"]["matched_fan_terms"][0]["fandom_of"] == "Shirayuki Hina fans"


def test_select_review_cases_skips_filtered_empty_targets_by_default():
    events = [
        _event(
            "자막 제공 배달의민족",
            "",
            index=1,
            status="filtered",
            severity="bad",
            flags=["empty_target"],
        ),
        _event(
            "찬나미들 천재야",
            "我們Chaenna們才是天才",
            index=2,
        ),
    ]

    cases = reviewer.select_review_cases(
        events,
        max_cases=1,
        control_cases=0,
    )

    assert len(cases) == 1
    assert cases[0].source_text == "찬나미들 천재야"


def test_parse_llm_reviews_accepts_fenced_json_and_normalizes():
    raw = """```json
[
  {
    "id": "case_0001",
    "severity": "BAD",
    "issue_type": "amount_error",
    "confidence": 1.2,
    "suggested_translation": "收到一萬五千元",
    "suggested_correction_rule": "amount mismatch",
    "reason_zh": "韓文是萬五千，不是五千。"
  }
]
```"""

    parsed = reviewer.parse_llm_reviews(raw)
    normalized = reviewer.normalize_review(parsed[0])

    assert normalized["id"] == "case_0001"
    assert normalized["severity"] == "bad"
    assert normalized["issue_type"] == "amount_error"
    assert normalized["confidence"] == 1.0


def test_normalize_review_accepts_glossary_gap_issue_type():
    normalized = reviewer.normalize_review({
        "id": "case_0001",
        "severity": "warn",
        "issue_type": "glossary_gap",
        "confidence": 0.8,
    })

    assert normalized["issue_type"] == "glossary_gap"


def test_main_dry_run_writes_selected_cases_without_api(tmp_path):
    events_path = tmp_path / "runtime_events.jsonl"
    _write_jsonl(
        events_path,
        [
            _event("안녕", "你好", index=1),
            _event(
                "찬나미들 천재야",
                "我們Chaenna們才是天才",
                index=2,
            ),
        ],
    )
    output_dir = tmp_path / "qa"

    result = reviewer.main([
        "--events",
        str(events_path),
        "--output-dir",
        str(output_dir),
        "--max-cases",
        "1",
        "--control-cases",
        "0",
        "--dry-run",
    ])

    assert result == 0
    selected = (output_dir / "selected_cases.jsonl").read_text(encoding="utf-8")
    assert "Chaenna們" in selected
    assert not (output_dir / "reviews.jsonl").exists()
    assert "no API call was made" in (output_dir / "report.md").read_text(encoding="utf-8")


def test_main_reviews_with_mocked_openrouter(tmp_path, monkeypatch):
    events_path = tmp_path / "runtime_events.jsonl"
    _write_jsonl(
        events_path,
        [_event("만 5천원 받았어", "收到五千", amount_mismatch_candidate=True)],
    )
    output_dir = tmp_path / "qa"

    def fake_call_openrouter(**kwargs):
        return json.dumps([
            {
                "id": "case_0001",
                "severity": "bad",
                "issue_type": "amount_error",
                "confidence": 0.95,
                "suggested_translation": "收到一萬五千元",
                "suggested_correction_rule": "review amount_mismatch_candidate",
                "reason_zh": "金額少了萬位。",
            }
        ], ensure_ascii=False)

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(reviewer, "call_openrouter", fake_call_openrouter)

    result = reviewer.main([
        "--events",
        str(events_path),
        "--output-dir",
        str(output_dir),
        "--max-cases",
        "1",
        "--control-cases",
        "0",
    ])

    assert result == 0
    rows = [
        json.loads(line)
        for line in (output_dir / "reviews.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["review_status"] == "reviewed"
    assert rows[0]["llm_review"]["severity"] == "bad"
    assert "收到一萬五千元" in (output_dir / "report.md").read_text(encoding="utf-8")
