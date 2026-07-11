from scripts.evaluate_japanese_retry import evaluate


def _event(index, retry=None):
    event = {
        "event_type": "translation",
        "created_at": str(index),
        "profile_id": "profile-a",
        "source_text": "원문",
        "target_text": "名字リナ",
        "quality_flags": ["target_has_japanese"],
        "quality_severity": "warn",
    }
    if retry is not None:
        event["quality_retry"] = retry
    return event


def test_gate_stays_closed_without_shadow_and_semantic_labels():
    report = evaluate([_event(1)])

    assert report["historical_japanese_flag_events"] == 1
    assert report["shadow_events"] == 0
    assert report["active_mode_decision"] == "no-go"


def test_gate_opens_only_when_every_requirement_is_met():
    events = [
        _event(
            index,
            {"trigger": "target_has_japanese", "would_replace": index < 20},
        )
        for index in range(30)
    ]
    labels = {str(index): "better" for index in range(30)}

    report = evaluate(events, labels)

    assert report["shadow_events"] == 30
    assert report["shadow_would_replace"] == 20
    assert report["active_mode_decision"] == "go"
