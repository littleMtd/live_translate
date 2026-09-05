import json

import pytest

from modules.profile_context import (
    ContentProfileConsensus,
    ProfileState,
    build_profile_identity_prompt,
    load_registry_snapshot,
    parse_profile_identity_evidence,
    parse_profile_identity_response,
    profile_state,
)


def _registry(tmp_path, profiles=None):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "common_stt_terms": ["shared"],
        "profiles": profiles or [
            {"profile_id": "", "label": "General"},
            {
                "profile_id": "url",
                "label": "UR:L",
                "aliases": ["URL"],
                "identity_markers": [
                    {"marker_id": "url_member_moka", "visible_names": ["모카", "Moka"]},
                ],
            },
            {"profile_id": "isegye_lilpa", "label": "Lilpa"},
        ],
    }), encoding="utf-8")
    return path


def test_confirmed_content_overrides_source_and_unknown_falls_back(tmp_path):
    registry = load_registry_snapshot(_registry(tmp_path), version=1)
    state = ProfileState(registry, source_profile_id="url")
    initial = state.current()
    confirmed = state.confirm_content("isegye_lilpa", confidence=0.9)
    fallback = state.clear_content("expired")
    assert initial.effective_profile_id == "url"
    assert confirmed.effective_profile_id == "isegye_lilpa"
    assert confirmed.generation == initial.generation + 1
    assert fallback.effective_profile_id == "url"
    assert fallback.generation == confirmed.generation + 1


def test_manual_mode_is_a_hard_effective_profile_lock(tmp_path):
    state = ProfileState(
        load_registry_snapshot(_registry(tmp_path), version=1),
        source_profile_id="url",
        mode="manual",
    )
    assert state.confirm_content("isegye_lilpa").effective_profile_id == "url"
    assert state.current().confirmation_state == "manual_locked"


def test_registry_reload_is_atomic_and_invalid_reload_rolls_back(tmp_path):
    first_path = _registry(tmp_path)
    state = ProfileState(load_registry_snapshot(first_path, version=1), source_profile_id="url")
    before = state.current()
    bad = tmp_path / "bad.json"
    bad.write_text('{"profiles": [{"profile_id": "url"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        state.reload_registry(bad)
    assert state.current() == before
    assert state.registry.version == 1

    second = _registry(tmp_path, [
        {"profile_id": "", "label": "General"},
        {"profile_id": "url", "label": "UR:L", "stt_terms": ["new"]},
        {"profile_id": "isegye_lilpa", "label": "Lilpa"},
    ])
    after = state.reload_registry(second)
    assert after.registry_version == 2
    assert after.generation == before.generation + 1
    assert state.registry.terms_for("url") == ("new",)


def test_allowlist_rejects_unknown_content_profile(tmp_path):
    state = ProfileState(
        load_registry_snapshot(_registry(tmp_path), version=1),
        source_profile_id="url",
    )
    with pytest.raises(ValueError):
        state.confirm_content("invented-person")


def test_identity_parser_is_exact_and_allowlisted(tmp_path):
    registry = load_registry_snapshot(_registry(tmp_path), version=1)
    prompt = build_profile_identity_prompt(registry)
    assert "primary sustained content across distinct frames" in prompt
    assert "isegye_lilpa|url" in prompt
    assert parse_profile_identity_response(
        '{"profile_id":"isegye_lilpa","matched_markers":[]}', registry
    ) == ("accepted", "isegye_lilpa")
    assert parse_profile_identity_response(
        '{"profile_id":"unknown","matched_markers":[]}', registry
    ) == ("unknown", "")
    for raw in (
        '{"profile_id":"Lilpa"}',
        '{"profile_id":"invented"}',
        '{"profile_id":"url","name":"x"}',
        '```json {"profile_id":"url"}```',
    ):
        assert parse_profile_identity_response(raw, registry) == ("rejected", "")


EXPECTED_MEMBER_MARKERS = {
    "url": {
        "url_member_moka": ("모카", "Moka"),
        "url_member_ranko": ("랑코", "Ranko"),
        "url_member_manyang": ("마냥", "Manyang"),
        "url_member_sommyang": ("솜먕", "Sommyang", "솜명", "솜명이", "솜명은"),
    },
    "isegye_lilpa": {
        "isegye_member_ine": ("아이네",),
        "isegye_member_jingburger": ("징버거",),
        "isegye_member_lilpa": ("릴파", "Lilpa"),
        "isegye_member_gosegu": ("고세구", "Gosegu"),
        "isegye_member_jururu": ("주르르", "Jururu"),
        "isegye_member_viichan": ("비챤",),
    },
    "hades_chxxnnx": {
        "hades_member_sompunch": ("솜주먹", "솜펀치", "Sompunch"),
        "hades_member_yeon_chorok": ("연초록", "Yeon Chorok"),
        "hades_member_chaenna": ("챈나", "Chxxnnx", "CHXXNNX", "Chaenna"),
        "hades_member_singgyul": ("띵귤", "싱귤", "TINGGYUL", "Singgyul"),
        "hades_member_kyma": ("키마", "큐마", "Kyma"),
    },
}

EXPECTED_BRAND_MARKERS = {
    "url": {
        "url_brand_group": (
            "UR:L", "유아렐", "유아엘", "YOU ARE LINKED", "결속아이돌", "결속 아이돌"
        ),
    },
    "isegye_lilpa": {
        "isegye_brand_group": ("이세계아이돌", "이세돌"),
    },
    "hades_chxxnnx": {
        "hades_brand_group": ("HADES", "하데스"),
    },
}


@pytest.mark.parametrize(
    ("profile_id", "marker_id", "visible_names"),
    [
        (profile_id, marker_id, visible_names)
        for profile_id, markers in EXPECTED_MEMBER_MARKERS.items()
        for marker_id, visible_names in markers.items()
    ],
)
def test_every_reviewed_member_marker_maps_to_its_profile(
    profile_id, marker_id, visible_names
):
    marker = profile_state.registry.marker(marker_id)
    assert marker is not None
    assert marker.profile_id == profile_id
    assert marker.visible_names == visible_names
    parsed = parse_profile_identity_evidence(
        json.dumps(
            {"profile_id": profile_id, "matched_markers": [marker_id]},
            separators=(",", ":"),
        ),
        profile_state.registry,
    )
    assert parsed.status == "accepted"
    assert parsed.strong


@pytest.mark.parametrize(
    ("profile_id", "marker_id", "visible_names"),
    [
        (profile_id, marker_id, visible_names)
        for profile_id, markers in EXPECTED_BRAND_MARKERS.items()
        for marker_id, visible_names in markers.items()
    ],
)
def test_every_reviewed_brand_marker_is_medium_strength(
    profile_id, marker_id, visible_names
):
    marker = profile_state.registry.marker(marker_id)
    assert marker is not None
    assert marker.profile_id == profile_id
    assert marker.visible_names == visible_names
    assert marker.strength == "medium"
    assert marker.kind == "group_brand"


def test_identity_parser_accepts_harmless_json_whitespace():
    parsed = parse_profile_identity_evidence(
        '{\n  "matched_markers": ["url_member_moka"],\n  "profile_id": "url"\n}',
        profile_state.registry,
    )
    assert parsed.status == "accepted"
    assert parsed.strong


def test_identity_parser_rejects_unsupplied_similar_or_cross_family_markers():
    registry = profile_state.registry
    for value in ("모카", "모카님", "url_member_mok", "chat:url_member_moka"):
        parsed = parse_profile_identity_evidence(
            json.dumps(
                {"profile_id": "url", "matched_markers": [value]},
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            registry,
        )
        assert parsed.status == "rejected"
    conflict = parse_profile_identity_evidence(
        '{"profile_id":"url","matched_markers":["url_member_moka","hades_member_kyma"]}',
        registry,
    )
    assert conflict.status == "conflict"


@pytest.mark.parametrize(
    "raw",
    (
        '[["profile_id","url"],["matched_markers",["url_member_moka"]]]',
        '[[],[]]',
    ),
)
def test_identity_parser_rejects_top_level_arrays_without_raising(raw):
    assert parse_profile_identity_evidence(raw, profile_state.registry).status == "rejected"


def test_registry_reload_replaces_identity_markers_atomically(tmp_path):
    first = _registry(tmp_path)
    state = ProfileState(load_registry_snapshot(first, version=1), source_profile_id="url")
    assert state.registry.marker("url_member_moka") is not None
    second = _registry(tmp_path, [
        {"profile_id": "", "label": "General"},
        {
            "profile_id": "url",
            "label": "UR:L",
            "identity_markers": [
                {"marker_id": "url_member_ranko", "visible_names": ["랑코"]},
            ],
        },
        {"profile_id": "isegye_lilpa", "label": "Lilpa"},
    ])
    state.reload_registry(second)
    assert state.registry.marker("url_member_moka") is None
    assert state.registry.marker("url_member_ranko").profile_id == "url"


def test_registry_rejects_prompt_shaped_ids_and_alias_id_collisions(tmp_path):
    with pytest.raises(ValueError):
        load_registry_snapshot(_registry(tmp_path, [
            {"profile_id": "", "label": "General"},
            {"profile_id": "bad|unknown", "label": "Bad"},
        ]), version=1)
    with pytest.raises(ValueError):
        load_registry_snapshot(_registry(tmp_path, [
            {"profile_id": "", "label": "General"},
            {"profile_id": "url", "label": "UR:L", "aliases": ["irise"]},
            {"profile_id": "irise", "label": "IRyS"},
        ]), version=1)


def test_content_consensus_requires_distinct_frames_and_resets_on_conflict():
    consensus = ContentProfileConsensus()
    assert consensus.observe("url", frame_key="a", window_generation=1) == (1, False, False, True)
    assert consensus.observe("url", frame_key="a", window_generation=1) == (1, False, False, False)
    assert consensus.observe("url", frame_key="b", window_generation=1) == (2, True, False, True)
    assert consensus.observe("isegye_lilpa", frame_key="c", window_generation=1) == (1, False, True, True)
    assert consensus.observe("isegye_lilpa", frame_key="d", window_generation=2) == (1, False, False, True)


def test_content_consensus_requires_profile_corroboration_for_confirmation():
    consensus = ContentProfileConsensus()
    assert consensus.observe(
        "url", frame_key="member-a", window_generation=1,
        profile_corroborated=False,
    ) == (1, False, False, True)
    assert consensus.observe(
        "url", frame_key="member-b", window_generation=1,
        profile_corroborated=False,
    ) == (2, False, False, True)
    assert consensus.observe(
        "url", frame_key="brand-c", window_generation=1,
        profile_corroborated=True,
    ) == (3, True, False, True)
