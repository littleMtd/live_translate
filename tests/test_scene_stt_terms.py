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
