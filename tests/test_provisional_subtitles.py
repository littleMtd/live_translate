from modules.provisional_subtitles import (
    ProvisionalCandidate,
    ProvisionalStore,
    provisional_fingerprint,
)


def _fingerprint(**overrides):
    values = {
        "prepared_source": "모카가 왔어요",
        "source_utterance_ids": ("utt-1",),
        "evidence_source_utterance_ids": ("utt-1",),
        "profile_id": "url",
        "activity_cache_identity": "activity-v1:chatting",
        "history_cohort": ("url", "chatting", 1),
        "messages": (("system", "prompt"), ("user", "모카가 왔어요")),
        "incomplete": True,
    }
    values.update(overrides)
    return provisional_fingerprint(**values)


def test_fingerprint_is_stable_and_every_contract_dimension_matters():
    baseline = _fingerprint()
    assert baseline == _fingerprint()
    variations = (
        {"prepared_source": "모카가 왔어요!"},
        {"source_utterance_ids": ("utt-2",)},
        {"evidence_source_utterance_ids": ("utt-2",)},
        {"profile_id": "irise"},
        {"activity_cache_identity": "activity-v1:singing"},
        {"history_cohort": ("url", "chatting", 2)},
        {"messages": (("system", "changed"),)},
        {"incomplete": False},
    )
    for variation in variations:
        assert _fingerprint(**variation) != baseline


def test_closed_store_rejects_late_preview_publish():
    store = ProvisionalStore()
    candidate = ProvisionalCandidate(
        provisional_id="preview-1",
        raw_target="摩卡來了",
        display_target="모카來了",
        fingerprint=_fingerprint(),
        engine="deepseek",
        model="flash",
        requested_at_monotonic=1.0,
        completed_at_monotonic=2.0,
        usage={},
        diagnostics={},
    )

    store.close("preview-1")

    assert not store.publish(candidate)
    assert store.candidate("preview-1") is None
    assert store.is_closed("preview-1")
