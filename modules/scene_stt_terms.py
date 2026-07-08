"""Scene-keyed STT hot vocabulary.

scene_context distills the screen into a short activity string
(cfg.translation.current_activity, e.g. "Pocket Monsters"). This module maps
that string to a set of Korean terms the STT prompt should bias toward, so
game vocabulary is heard correctly at the source instead of being repaired
after a mishear (observed: 메가태화←메가진화, 픽생몬←포켓몬).

Deliberately config-free (config.py imports modules.streamer_profiles, so
this layer must not import config); callers pass the activity string in.
"""

from __future__ import annotations

import json
from pathlib import Path

from modules.streamer_profiles import profile_stt_terms

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "scene_stt_terms.json"


def _load_rules(path: Path = _DATA_PATH) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    rules = []
    for rule in raw.get("rules", []):
        keywords = tuple(str(k).strip().lower() for k in rule.get("match", []) if str(k).strip())
        terms = tuple(str(t).strip() for t in rule.get("terms", []) if str(t).strip())
        profile_refs_raw = rule.get("profile_terms", [])
        if isinstance(profile_refs_raw, str):
            profile_refs = (profile_refs_raw,)
        elif isinstance(profile_refs_raw, list):
            profile_refs = tuple(str(ref).strip() for ref in profile_refs_raw if str(ref).strip())
        else:
            profile_refs = ()
        profile_terms = tuple(
            term
            for profile_ref in profile_refs
            for term in profile_stt_terms(profile_ref)
        )
        terms = (*terms, *profile_terms)
        if keywords and terms:
            rules.append((keywords, terms))
    return tuple(rules)


_RULES = _load_rules()


def terms_for_activity(activity: str) -> tuple[str, ...]:
    """Korean STT bias terms for the current on-screen activity ('' → none)."""
    needle = (activity or "").strip().lower()
    if not needle:
        return ()
    collected: list[str] = []
    for keywords, terms in _RULES:
        if any(keyword in needle for keyword in keywords):
            collected.extend(terms)
    # preserve order, drop duplicates across overlapping rules
    return tuple(dict.fromkeys(collected))
