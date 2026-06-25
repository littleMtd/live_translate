from scripts.cluster_name_variants import (
    DEFAULT_STOP_LIST,
    build_table,
    cluster_tokens,
    edit_distance,
    jamo_distance,
    render_review_markdown,
    to_jamo,
)


def test_to_jamo_decomposes_syllables_and_passes_through_other():
    assert to_jamo("채나") == ["ㅊ", "ㅐ", "ㄴ", "ㅏ"]
    # tail consonant is emitted
    assert to_jamo("챈") == ["ㅊ", "ㅐ", "ㄴ"]
    assert to_jamo("a?") == ["a", "?"]


def test_jamo_distance_close_for_one_spelling_variant():
    near_abs, near_norm = jamo_distance("채나", "채냐")
    far_abs, far_norm = jamo_distance("채나", "치즈")
    assert near_abs == 1
    assert near_norm < far_norm


def test_edit_distance_basic():
    assert edit_distance([], ["a"]) == 1
    assert edit_distance(["a", "b"], ["a", "b"]) == 0
    assert edit_distance(["a", "b"], ["a", "c"]) == 1


def test_cluster_tokens_groups_variants_and_isolates_distant_token():
    counts = {"채나": 19, "채냐": 4, "치즈": 5}
    clusters, boundary = cluster_tokens(
        list(counts), counts, norm_threshold=0.34
    )
    # one cluster has the two spelling variants, ordered by count desc
    variant_cluster = next(c for c in clusters if "채나" in c)
    assert variant_cluster == ["채나", "채냐"]
    # the distant token stays alone
    assert ["치즈"] in clusters
    # boundary pairs carry numeric distances for human review of the threshold
    assert all("normalized" in p and "relation" in p for p in boundary)


def test_build_table_excludes_v1_from_candidate_counts(monkeypatch):
    monkeypatch.setattr(
        "scripts.cluster_name_variants.build_hangul_allowlist",
        lambda: (frozenset(), frozenset()),
    )
    events = [
        {
            "schema_version": 1,
            "status": "success",
            "profile_id": "profile-a",
            "source_text": "구버전",
            "target_text": "채나",
        },
        {
            "schema_version": 2,
            "status": "success",
            "profile_id": "profile-a",
            "run_id": "run-1",
            "sequence_id": 1,
            "source_text": "챈나가 왔어요",
            "target_text": "채냐來了",
        },
    ]

    table = build_table(
        events=events,
        min_count=1,
        norm_threshold=0.34,
        stop_list=DEFAULT_STOP_LIST,
    )

    assert table["schema_version_coverage"]["schema_v1_or_missing"] == 1
    assert table["profiles"]["profile-a"]["leak_instances"] == 1
    singleton = table["profiles"]["profile-a"]["singletons"][0]
    assert singleton["token"] == "채냐"
    assert singleton["examples"][0]["run_id"] == "run-1"
    assert table["measurement"]["redetected_after_rewrite"] is None


def test_render_review_markdown_exposes_gate_and_examples(monkeypatch):
    monkeypatch.setattr(
        "scripts.cluster_name_variants.build_hangul_allowlist",
        lambda: (frozenset(), frozenset()),
    )
    events = [
        {
            "schema_version": 2,
            "status": "success",
            "profile_id": "profile-a",
            "source_text": "챈나가 왔어요",
            "target_text": "채나來了",
        },
        {
            "schema_version": 2,
            "status": "success",
            "profile_id": "profile-a",
            "source_text": "챈나가 웃어요",
            "target_text": "채냐在笑",
        },
    ]
    table = build_table(
        events=events,
        min_count=1,
        norm_threshold=0.34,
        stop_list=DEFAULT_STOP_LIST,
    )

    markdown = render_review_markdown(table)

    assert "schema v2 only" in markdown
    assert "Canonical Korean source" in markdown
    assert "챈나가 왔어요" in markdown
