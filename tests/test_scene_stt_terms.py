import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.scene_stt_terms import terms_for_activity
from modules.streamer_profiles import build_stt_glossary


def test_pokemon_activities_map_to_pokemon_terms():
    # vision produced all of these labels for the same Pokémon-server arc
    for activity in ("Pocket Monsters", "Hades Pocket Roguelike", "pokemon raid"):
        terms = terms_for_activity(activity)
        assert "포켓몬" in terms, activity
        assert "메가진화" in terms  # the observed 메가태화 mishear target


def test_hades_activity_reuses_hades_profile_terms():
    terms = terms_for_activity("Hades")

    assert "챈나" in terms
    assert "하데스 오락실" in terms
    assert terms == terms_for_activity("hades_chxxnnx")


def test_game_scene_terms_come_before_profile_terms_for_mixed_activity():
    terms = terms_for_activity("Hades Pocket Roguelike")

    assert terms.index("메가진화") < terms.index("챈나")


def test_broad_roguelike_activity_does_not_inject_pokemon_terms():
    terms = terms_for_activity("Hades Roguelike")

    assert "챈나" in terms
    assert "메가진화" not in terms


def test_unrelated_or_empty_activity_yields_no_terms():
    assert terms_for_activity("just chatting") == ()
    assert terms_for_activity("coding") == ()
    assert terms_for_activity("") == ()
    assert terms_for_activity("watching a video") == ()


def test_matching_is_case_insensitive():
    assert terms_for_activity("POKEMON") == terms_for_activity("pokemon")
    assert terms_for_activity("Minecraft server") != ()


def test_glossary_merges_scene_terms_and_dedupes():
    base = build_stt_glossary("stellive_hina")
    merged = build_stt_glossary("stellive_hina", extra_terms=("포켓몬", "메가진화"))
    assert "포켓몬" in merged and "메가진화" in merged
    assert merged != base
    # a term the profile already carries must not be duplicated
    already = build_stt_glossary("stellive_hina", extra_terms=("히나",))
    assert already.count("히나") == base.count("히나")


def test_glossary_without_extra_terms_is_unchanged():
    assert build_stt_glossary("stellive_hina", extra_terms=()) == \
        build_stt_glossary("stellive_hina")


def test_hades_profile_alias_uses_canonical_stt_terms():
    assert build_stt_glossary("hades") == build_stt_glossary("hades_chxxnnx")
