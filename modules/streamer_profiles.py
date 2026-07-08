import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StreamerProfile:
    profile_id: str
    label: str
    stt_terms: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


_PROFILE_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "streamer_profiles.json"


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _load_profile_data(
    path: Path = _PROFILE_DATA_PATH,
) -> tuple[tuple[str, ...], dict[str, StreamerProfile], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("streamer profile data must be a JSON object")

    common_terms = _string_tuple(data.get("common_stt_terms"), "common_stt_terms")
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("profiles must be a list")

    profile_ids_lower = {
        str(raw_profile.get("profile_id")).lower(): str(raw_profile.get("profile_id"))
        for raw_profile in raw_profiles
        if isinstance(raw_profile, dict) and isinstance(raw_profile.get("profile_id"), str)
    }
    profiles: dict[str, StreamerProfile] = {}
    aliases: dict[str, str] = {}
    for index, raw_profile in enumerate(raw_profiles):
        if not isinstance(raw_profile, dict):
            raise ValueError(f"profiles[{index}] must be an object")

        profile_id = raw_profile.get("profile_id")
        label = raw_profile.get("label")
        if not isinstance(profile_id, str):
            raise ValueError(f"profiles[{index}].profile_id must be a string")
        if not isinstance(label, str):
            raise ValueError(f"profiles[{index}].label must be a string")
        if profile_id in profiles:
            raise ValueError(f"duplicate streamer profile id: {profile_id}")

        profile_aliases = _string_tuple(raw_profile.get("aliases"), f"profiles[{index}].aliases")
        profiles[profile_id] = StreamerProfile(
            profile_id=profile_id,
            label=label,
            stt_terms=_string_tuple(raw_profile.get("stt_terms"), f"profiles[{index}].stt_terms"),
            aliases=profile_aliases,
        )
        for alias in profile_aliases:
            normalized = alias.strip().lower()
            if normalized:
                if normalized in aliases:
                    raise ValueError(f"duplicate streamer profile alias: {alias}")
                conflicting_profile_id = profile_ids_lower.get(normalized)
                if conflicting_profile_id is not None and conflicting_profile_id != profile_id:
                    raise ValueError(
                        f"streamer profile alias conflicts with profile id: {alias}"
                    )
                aliases[normalized] = profile_id

    if "" not in profiles:
        raise ValueError("streamer profile data must include the general profile with id ''")

    return common_terms, profiles, aliases


_COMMON_STT_TERMS, _PROFILES, _PROFILE_ALIASES = _load_profile_data()


def canonical_profile_id(profile_id: str) -> str:
    key = (profile_id or "").strip()
    if key in _PROFILES:
        return key
    return _PROFILE_ALIASES.get(key.lower(), "")


def get_profile(profile_id: str) -> StreamerProfile:
    canonical = canonical_profile_id(profile_id)
    return _PROFILES.get(canonical, _PROFILES[""])


def known_profile_ids(*, include_aliases: bool = False) -> frozenset[str]:
    ids = set(_PROFILES)
    if include_aliases:
        ids.update(_PROFILE_ALIASES)
    return frozenset(ids)


def profile_stt_terms(profile_id: str) -> tuple[str, ...]:
    return get_profile(profile_id).stt_terms


def build_stt_glossary(profile_id: str, include_common: bool = True,
                       extra_terms: tuple[str, ...] = ()) -> str:
    """Profile STT bias terms, optionally merged with scene-keyed hot terms
    (modules/scene_stt_terms) so on-screen game vocabulary is heard right."""
    profile = get_profile(profile_id)
    # Runtime scene terms are the most time-sensitive STT bias terms. Put them
    # first so Groq prompt-budget truncation preserves the current-game hot
    # words before static common/profile vocabulary.
    terms = ((*_COMMON_STT_TERMS, *profile.stt_terms)
             if include_common else profile.stt_terms)
    terms = (*extra_terms, *terms)
    unique_terms = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    if not unique_terms:
        return ""
    return "Prefer exact spellings for names and terms: " + ", ".join(unique_terms) + "."
