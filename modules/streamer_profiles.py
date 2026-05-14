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


def _load_profile_data(path: Path = _PROFILE_DATA_PATH) -> tuple[tuple[str, ...], dict[str, StreamerProfile]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("streamer profile data must be a JSON object")

    common_terms = _string_tuple(data.get("common_stt_terms"), "common_stt_terms")
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("profiles must be a list")

    profiles: dict[str, StreamerProfile] = {}
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

        profiles[profile_id] = StreamerProfile(
            profile_id=profile_id,
            label=label,
            stt_terms=_string_tuple(raw_profile.get("stt_terms"), f"profiles[{index}].stt_terms"),
            aliases=_string_tuple(raw_profile.get("aliases"), f"profiles[{index}].aliases"),
        )

    if "" not in profiles:
        raise ValueError("streamer profile data must include the general profile with id ''")

    return common_terms, profiles


_COMMON_STT_TERMS, _PROFILES = _load_profile_data()


def get_profile(profile_id: str) -> StreamerProfile:
    return _PROFILES.get(profile_id, _PROFILES[""])


def known_profile_ids() -> frozenset[str]:
    return frozenset(_PROFILES)


def build_stt_glossary(profile_id: str, include_common: bool = True) -> str:
    profile = get_profile(profile_id)
    terms = ((*_COMMON_STT_TERMS, *profile.stt_terms)
             if include_common else profile.stt_terms)
    unique_terms = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    if not unique_terms:
        return ""
    return "Prefer exact spellings for names and terms: " + ", ".join(unique_terms) + "."
