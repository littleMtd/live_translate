"""Generation-safe source/content profile state for the live pipeline."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
from typing import Iterator


_PROFILE_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_MARKER_ID_RE = re.compile(r"^[a-z0-9_]{1,96}$")
_MARKER_STRENGTHS = frozenset({"strong", "medium", "weak"})
_MARKER_STRENGTH_RANK = {"weak": 1, "medium": 2, "strong": 3}


@dataclass(frozen=True)
class ProfileIdentityMarker:
    marker_id: str
    profile_id: str
    visible_names: tuple[str, ...]
    strength: str = "strong"
    kind: str = "member_name"


@dataclass(frozen=True)
class ParsedProfileIdentity:
    status: str
    profile_id: str = ""
    matched_markers: tuple[str, ...] = ()
    marker_strengths: tuple[str, ...] = ()
    rejection_reason: str = ""

    @property
    def strong(self) -> bool:
        return "strong" in self.marker_strengths

    @property
    def evidence_strength(self) -> str:
        return max(
            self.marker_strengths,
            key=lambda value: _MARKER_STRENGTH_RANK[value],
            default="none",
        )


class _JsonObjectPairs(list):
    """Keep JSON objects distinguishable from JSON arrays during strict parsing."""


@dataclass(frozen=True)
class ProfileRegistrySnapshot:
    version: int
    identity: str
    profile_ids: frozenset[str]
    aliases: tuple[tuple[str, str], ...]
    common_stt_terms: tuple[str, ...]
    profile_stt_terms: tuple[tuple[str, tuple[str, ...]], ...]
    identity_markers: tuple[ProfileIdentityMarker, ...]

    def canonical_id(self, value: object) -> str | None:
        key = str(value or "").strip()
        if key in self.profile_ids:
            return key
        folded = key.casefold()
        return dict(self.aliases).get(folded)

    def terms_for(self, profile_id: object) -> tuple[str, ...]:
        canonical = self.canonical_id(profile_id)
        return dict(self.profile_stt_terms).get(canonical or "", ())

    def markers_for(self, profile_id: object) -> tuple[ProfileIdentityMarker, ...]:
        canonical = self.canonical_id(profile_id)
        return tuple(
            marker for marker in self.identity_markers
            if marker.profile_id == (canonical or "")
        )

    def marker(self, marker_id: object) -> ProfileIdentityMarker | None:
        key = str(marker_id or "").strip()
        return next(
            (marker for marker in self.identity_markers if marker.marker_id == key),
            None,
        )


def load_registry_snapshot(
    path: Path,
    *,
    version: int,
) -> ProfileRegistrySnapshot:
    raw_bytes = path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise ValueError("streamer profile data must contain a profiles list")
    common = data.get("common_stt_terms", [])
    if not isinstance(common, list) or not all(isinstance(x, str) for x in common):
        raise ValueError("common_stt_terms must be a list of strings")
    ids: set[str] = set()
    aliases: dict[str, str] = {}
    terms: list[tuple[str, tuple[str, ...]]] = []
    markers: list[ProfileIdentityMarker] = []
    marker_ids: set[str] = set()
    visible_name_owners: dict[str, str] = {}
    rows = data["profiles"]
    if not 1 <= len(rows) <= 64:
        raise ValueError("profiles must contain between 1 and 64 reviewed entries")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"profiles[{index}] must be an object")
        profile_id = row.get("profile_id")
        if (
            not isinstance(profile_id, str)
            or profile_id in ids
            or (profile_id and not _PROFILE_ID_RE.fullmatch(profile_id))
        ):
            raise ValueError(f"invalid or duplicate profile id: {profile_id!r}")
        if not isinstance(row.get("label"), str) or not row["label"].strip():
            raise ValueError(f"profiles[{index}].label must be a non-empty string")
        ids.add(profile_id)
        row_terms = row.get("stt_terms", [])
        row_aliases = row.get("aliases", [])
        row_markers = row.get("identity_markers", [])
        if not isinstance(row_terms, list) or not all(isinstance(x, str) for x in row_terms):
            raise ValueError(f"profiles[{index}].stt_terms must be strings")
        if not isinstance(row_aliases, list) or not all(isinstance(x, str) for x in row_aliases):
            raise ValueError(f"profiles[{index}].aliases must be strings")
        if not isinstance(row_markers, list):
            raise ValueError(f"profiles[{index}].identity_markers must be a list")
        terms.append((profile_id, tuple(row_terms)))
        for marker_index, marker in enumerate(row_markers):
            if not isinstance(marker, dict):
                raise ValueError(
                    f"profiles[{index}].identity_markers[{marker_index}] must be an object"
                )
            marker_id = marker.get("marker_id")
            visible_names = marker.get("visible_names")
            strength = marker.get("strength", "strong")
            kind = marker.get("kind", "member_name")
            if (
                not isinstance(marker_id, str)
                or not _MARKER_ID_RE.fullmatch(marker_id)
                or marker_id in marker_ids
            ):
                raise ValueError(f"invalid or duplicate identity marker id: {marker_id!r}")
            if (
                not isinstance(visible_names, list)
                or not visible_names
                or not all(isinstance(name, str) and name.strip() for name in visible_names)
            ):
                raise ValueError(f"identity marker {marker_id!r} must have visible names")
            if strength not in _MARKER_STRENGTHS:
                raise ValueError(f"identity marker {marker_id!r} has invalid strength")
            if not isinstance(kind, str) or not _PROFILE_ID_RE.fullmatch(kind):
                raise ValueError(f"identity marker {marker_id!r} has invalid kind")
            reviewed_names = tuple(dict.fromkeys(name.strip() for name in visible_names))
            for name in reviewed_names:
                folded = name.casefold()
                owner = visible_name_owners.get(folded)
                if owner is not None and owner != profile_id:
                    raise ValueError(f"identity marker visible name conflicts across profiles: {name!r}")
                visible_name_owners[folded] = profile_id
            marker_ids.add(marker_id)
            markers.append(
                ProfileIdentityMarker(
                    marker_id,
                    profile_id,
                    reviewed_names,
                    strength,
                    kind,
                )
            )
        for alias in row_aliases:
            key = alias.strip().casefold()
            if not key:
                continue
            if key in aliases:
                raise ValueError(f"duplicate/conflicting profile alias: {alias}")
            aliases[key] = profile_id
    if "" not in ids:
        raise ValueError("registry must include the general profile")
    folded_ids = {profile_id.casefold(): profile_id for profile_id in ids if profile_id}
    for alias, owner in aliases.items():
        if alias in folded_ids and folded_ids[alias] != owner:
            raise ValueError(f"profile alias conflicts with profile id: {alias}")
    return ProfileRegistrySnapshot(
        version=version,
        identity=f"profiles:{version}:{sha256(raw_bytes).hexdigest()[:16]}",
        profile_ids=frozenset(ids),
        aliases=tuple(sorted(aliases.items())),
        common_stt_terms=tuple(common),
        profile_stt_terms=tuple(sorted(terms)),
        identity_markers=tuple(sorted(markers, key=lambda marker: marker.marker_id)),
    )


@dataclass(frozen=True)
class ProfileSnapshot:
    source_profile_id: str
    content_profile_id: str = ""
    effective_profile_id: str = ""
    generation: int = 0
    registry_identity: str = ""
    registry_version: int = 0
    evidence_source: str = "source_default"
    confidence: float | None = None
    confirmation_state: str = "source_fallback"
    mode: str = "auto"
    translation_profile_applied: bool = True
    stt_glossary_applied: bool = True
    registry: ProfileRegistrySnapshot | None = None

    @property
    def cache_identity(self) -> str:
        return (
            f"{self.registry_identity}:{self.generation}:"
            f"{int(self.translation_profile_applied)}:"
            f"{int(self.stt_glossary_applied)}:{self.effective_profile_id}"
        )

    def as_metadata(self) -> dict[str, object]:
        return {
            "source_profile_id": self.source_profile_id,
            "content_profile_id": self.content_profile_id,
            "effective_profile_id": self.effective_profile_id,
            "profile_id": self.effective_profile_id,
            "profile_generation": self.generation,
            "profile_registry_identity": self.registry_identity,
            "profile_registry_version": self.registry_version,
            "profile_evidence_source": self.evidence_source,
            "profile_confidence": self.confidence,
            "profile_confirmation_state": self.confirmation_state,
            "profile_mode": self.mode,
            "profile_applied": self.translation_profile_applied,
            "profile_glossary_applied": self.stt_glossary_applied,
            "profile_cache_identity": self.cache_identity,
        }


class ProfileState:
    """Atomically owns one registry and one effective live profile snapshot."""

    def __init__(
        self,
        registry: ProfileRegistrySnapshot,
        *,
        source_profile_id: str,
        mode: str = "auto",
        translation_profile_applied: bool = True,
        stt_glossary_applied: bool = True,
        clock=time.monotonic,
    ):
        self._lock = threading.RLock()
        self._clock = clock
        self._registry = registry
        self._generation = 0
        self._confirmed_at: float | None = None
        self._snapshot = self._build(
            source_profile_id=source_profile_id,
            content_profile_id="",
            mode=mode,
            evidence_source="manual_hard_lock" if mode == "manual" else "source_default",
            confidence=1.0 if mode == "manual" else None,
            confirmation_state="manual_locked" if mode == "manual" else "source_fallback",
            translation_profile_applied=translation_profile_applied,
            stt_glossary_applied=stt_glossary_applied,
        )

    def _canonical_required(self, value: object) -> str:
        canonical = self._registry.canonical_id(value)
        if canonical is None:
            raise ValueError(f"unknown profile id: {value!r}")
        return canonical

    def _build(self, **values) -> ProfileSnapshot:
        source = self._canonical_required(values["source_profile_id"])
        content_raw = values.get("content_profile_id", "")
        content = self._canonical_required(content_raw) if content_raw else ""
        mode = values.get("mode", "auto")
        if mode not in {"auto", "manual"}:
            raise ValueError("profile mode must be auto or manual")
        effective = source if mode == "manual" or not content else content
        return ProfileSnapshot(
            source_profile_id=source,
            content_profile_id="" if mode == "manual" else content,
            effective_profile_id=effective,
            generation=self._generation,
            registry_identity=self._registry.identity,
            registry_version=self._registry.version,
            evidence_source=values.get("evidence_source", "source_default"),
            confidence=values.get("confidence"),
            confirmation_state=values.get("confirmation_state", "source_fallback"),
            mode=mode,
            translation_profile_applied=bool(values.get("translation_profile_applied", True)),
            stt_glossary_applied=bool(values.get("stt_glossary_applied", True)),
            registry=self._registry,
        )

    def current(self) -> ProfileSnapshot:
        with self._lock:
            return self._snapshot

    def legacy_snapshot(
        self,
        profile_id: object,
        *,
        translation_profile_applied: bool = True,
        stt_glossary_applied: bool = True,
    ) -> ProfileSnapshot:
        """Build a request-local compatibility snapshot without publishing it."""
        with self._lock:
            raw = profile_id if isinstance(profile_id, str) else ""
            canonical = self._registry.canonical_id(raw)
            if canonical is None:
                # Typed production snapshots are always allowlisted. This
                # compatibility branch preserves synthetic/legacy test events.
                canonical = raw.strip()
            return ProfileSnapshot(
                source_profile_id=canonical,
                content_profile_id="",
                effective_profile_id=canonical,
                generation=self._snapshot.generation,
                registry_identity=self._registry.identity,
                registry_version=self._registry.version,
                evidence_source="legacy_fallback",
                confidence=None,
                confirmation_state="source_fallback",
                mode="manual",
                translation_profile_applied=translation_profile_applied,
                stt_glossary_applied=stt_glossary_applied,
                registry=self._registry,
            )

    @property
    def registry(self) -> ProfileRegistrySnapshot:
        with self._lock:
            return self._registry

    def configure_source(
        self,
        source_profile_id: str,
        *,
        mode: str | None = None,
        translation_profile_applied: bool | None = None,
        stt_glossary_applied: bool | None = None,
    ) -> ProfileSnapshot:
        with self._lock:
            old = self._snapshot
            next_mode = mode or old.mode
            self._generation += 1
            self._snapshot = self._build(
                source_profile_id=source_profile_id,
                content_profile_id=old.content_profile_id if next_mode == "auto" else "",
                mode=next_mode,
                evidence_source="manual_hard_lock" if next_mode == "manual" else "source_default",
                confidence=1.0 if next_mode == "manual" else old.confidence,
                confirmation_state="manual_locked" if next_mode == "manual" else "source_fallback",
                translation_profile_applied=(
                    old.translation_profile_applied
                    if translation_profile_applied is None
                    else translation_profile_applied
                ),
                stt_glossary_applied=(
                    old.stt_glossary_applied
                    if stt_glossary_applied is None
                    else stt_glossary_applied
                ),
            )
            return self._snapshot

    def confirm_content(self, profile_id: str, *, confidence: float = 1.0, evidence_source: str = "scene_vision") -> ProfileSnapshot:
        with self._lock:
            if self._snapshot.mode == "manual":
                return self._snapshot
            canonical = self._canonical_required(profile_id)
            if not canonical:
                return self.clear_content("unknown")
            if canonical == self._snapshot.content_profile_id:
                self._confirmed_at = self._clock()
                return self._snapshot
            self._generation += 1
            old = self._snapshot
            self._confirmed_at = self._clock()
            self._snapshot = self._build(
                source_profile_id=old.source_profile_id,
                content_profile_id=canonical,
                mode=old.mode,
                evidence_source=evidence_source,
                confidence=confidence,
                confirmation_state="confirmed",
                translation_profile_applied=old.translation_profile_applied,
                stt_glossary_applied=old.stt_glossary_applied,
            )
            return self._snapshot

    def clear_content(self, reason: str = "unknown") -> ProfileSnapshot:
        with self._lock:
            old = self._snapshot
            if old.mode == "manual" or not old.content_profile_id:
                return old
            self._generation += 1
            self._confirmed_at = None
            self._snapshot = self._build(
                source_profile_id=old.source_profile_id,
                content_profile_id="",
                mode=old.mode,
                evidence_source=reason,
                confidence=None,
                confirmation_state="source_fallback",
                translation_profile_applied=old.translation_profile_applied,
                stt_glossary_applied=old.stt_glossary_applied,
            )
            return self._snapshot

    def reload_registry(self, path: Path) -> ProfileSnapshot:
        with self._lock:
            candidate = load_registry_snapshot(path, version=self._registry.version + 1)
            source = candidate.canonical_id(self._snapshot.source_profile_id)
            content = candidate.canonical_id(self._snapshot.content_profile_id) if self._snapshot.content_profile_id else ""
            if source is None or (self._snapshot.content_profile_id and content is None):
                raise ValueError("reload removes an active profile")
            self._registry = candidate
            self._generation += 1
            old = self._snapshot
            self._snapshot = self._build(
                source_profile_id=source,
                content_profile_id=content or "",
                mode=old.mode,
                evidence_source="registry_reload",
                confidence=old.confidence,
                confirmation_state=old.confirmation_state,
                translation_profile_applied=old.translation_profile_applied,
                stt_glossary_applied=old.stt_glossary_applied,
            )
            return self._snapshot


_BOUND_PROFILE_SNAPSHOT: ContextVar[ProfileSnapshot | None] = ContextVar(
    "live_translate_profile_snapshot", default=None
)


@contextmanager
def bind_profile_snapshot(snapshot: ProfileSnapshot) -> Iterator[ProfileSnapshot]:
    token = _BOUND_PROFILE_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _BOUND_PROFILE_SNAPSHOT.reset(token)


def bound_profile_snapshot() -> ProfileSnapshot | None:
    return _BOUND_PROFILE_SNAPSHOT.get()


def effective_profile_applied(fallback: bool = True) -> bool:
    snapshot = bound_profile_snapshot()
    return snapshot.translation_profile_applied if snapshot is not None else bool(fallback)


_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "streamer_profiles.json"
_INITIAL_REGISTRY = load_registry_snapshot(_DEFAULT_PATH, version=1)
profile_state = ProfileState(_INITIAL_REGISTRY, source_profile_id="", mode="auto")


PROFILE_IDENTITY_PROMPT = (
    "Identify which reviewed content profile is persistently represented in this "
    "livestream player crop. Use stable avatar identity, group branding, logos, "
    "persistent nameplates, overlays, and characteristic layout; do not switch for "
    "a transient guest, chat message, donation, or isolated mention. A clip or "
    "reaction may qualify only when the reviewed identity is the primary sustained "
    "content across distinct frames, not merely a passing appearance. "
    "Exact visible member names listed below are authoritative strong evidence. "
    "Return a marker only when its exact reviewed visible name is clearly readable "
    "inside the supplied player crop; never claim a marker from appearance, similar "
    "text, a longer unrelated string that merely contains it, chat usernames, "
    "donations, browser chrome, or unrelated comments. "
    "If more than one listed member is visible, return every matching marker. "
    "Return exactly one minified JSON object with these two keys: "
    '{{"profile_id":"<reviewed-id-or-unknown>","matched_markers":["<marker-id>"]}}. '
    "Allowed reviewed IDs: {allowed_ids}. If evidence is weak, conflicting, or not "
    "one of those IDs, use profile_id unknown. Use only marker IDs supplied here, "
    "and use an empty marker list for weaker visual evidence. Reviewed markers: "
    "{identity_markers}. No prose."
)


def build_profile_identity_prompt(registry: ProfileRegistrySnapshot) -> str:
    allowed = "|".join(sorted(item for item in registry.profile_ids if item))
    markers = "; ".join(
        f"{marker.marker_id}={marker.profile_id}:{marker.strength}:{marker.kind}"
        f"[{'|'.join(marker.visible_names)}]"
        for marker in registry.identity_markers
    ) or "none"
    return PROFILE_IDENTITY_PROMPT.format(
        allowed_ids=allowed,
        identity_markers=markers,
    )


def build_registry_stt_glossary(
    registry: ProfileRegistrySnapshot,
    profile_id: str,
    *,
    extra_terms: tuple[str, ...] = (),
) -> str:
    terms = (*extra_terms, *registry.common_stt_terms, *registry.terms_for(profile_id))
    unique = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    return (
        "Prefer exact spellings for names and terms: " + ", ".join(unique) + "."
        if unique else ""
    )


def parse_profile_identity_evidence(
    raw: object,
    registry: ProfileRegistrySnapshot,
) -> ParsedProfileIdentity:
    if not isinstance(raw, str):
        return ParsedProfileIdentity("rejected", rejection_reason="invalid_response_type")
    try:
        pairs = json.loads(raw.strip(), object_pairs_hook=_JsonObjectPairs)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ParsedProfileIdentity("rejected", rejection_reason="invalid_json")
    if not isinstance(pairs, _JsonObjectPairs) or len(pairs) != 2:
        return ParsedProfileIdentity("rejected", rejection_reason="invalid_schema")
    if any(
        not isinstance(pair, (list, tuple))
        or len(pair) != 2
        or not isinstance(pair[0], str)
        for pair in pairs
    ):
        return ParsedProfileIdentity("rejected", rejection_reason="invalid_schema")
    keys = [pair[0] for pair in pairs]
    if len(set(keys)) != 2 or set(keys) != {"profile_id", "matched_markers"}:
        return ParsedProfileIdentity("rejected", rejection_reason="invalid_schema")
    values = dict(pairs)
    value, raw_markers = values["profile_id"], values["matched_markers"]
    if (
        not isinstance(value, str)
        or not isinstance(raw_markers, list)
        or not all(isinstance(marker_id, str) for marker_id in raw_markers)
    ):
        return ParsedProfileIdentity("rejected", rejection_reason="invalid_schema")
    marker_ids = tuple(raw_markers)
    if len(marker_ids) != len(set(marker_ids)):
        return ParsedProfileIdentity("rejected", rejection_reason="duplicate_markers")
    markers = tuple(registry.marker(marker_id) for marker_id in marker_ids)
    if any(marker is None for marker in markers):
        return ParsedProfileIdentity("rejected", rejection_reason="unsupported_marker")
    candidate = value.strip()
    if candidate == "unknown":
        if marker_ids:
            return ParsedProfileIdentity("rejected", rejection_reason="unknown_with_markers")
        return ParsedProfileIdentity("unknown")
    # Vision must return a canonical reviewed ID, never an alias.
    if candidate not in registry.profile_ids or not candidate:
        return ParsedProfileIdentity("rejected", rejection_reason="unsupported_profile")
    marker_profiles = {marker.profile_id for marker in markers if marker is not None}
    marker_strengths = tuple(
        marker.strength for marker in markers if marker is not None
    )
    if marker_profiles and (len(marker_profiles) != 1 or candidate not in marker_profiles):
        return ParsedProfileIdentity(
            "conflict",
            candidate,
            marker_ids,
            marker_strengths,
            "cross_family_or_profile_mismatch",
        )
    return ParsedProfileIdentity(
        "accepted",
        candidate,
        marker_ids,
        marker_strengths,
    )


def parse_profile_identity_response(
    raw: object,
    registry: ProfileRegistrySnapshot,
) -> tuple[str, str]:
    """Compatibility projection for callers that do not consume marker evidence."""
    parsed = parse_profile_identity_evidence(raw, registry)
    return parsed.status, parsed.profile_id


class ContentProfileConsensus:
    """Two distinct frames in one window generation activate a profile."""

    def __init__(self):
        self._window_generation = -1
        self._candidate = ""
        self._frame_keys: set[str] = set()

    def reset(self, window_generation: int = -1) -> None:
        self._window_generation = window_generation
        self._candidate = ""
        self._frame_keys.clear()

    def observe(
        self,
        profile_id: str,
        *,
        frame_key: str,
        window_generation: int,
    ) -> tuple[int, bool, bool, bool]:
        if window_generation != self._window_generation:
            self.reset(window_generation)
        conflict = bool(self._candidate and profile_id != self._candidate)
        if conflict:
            self._candidate = profile_id
            self._frame_keys = {frame_key}
            return 1, False, True, True
        if not self._candidate:
            self._candidate = profile_id
        reused = frame_key in self._frame_keys
        self._frame_keys.add(frame_key)
        streak = len(self._frame_keys)
        return streak, streak >= 2 and not reused, False, not reused


class ProfileResolutionStatusStore:
    """Latest privacy-safe resolver observation for dashboard status."""

    def __init__(self):
        self._lock = threading.RLock()
        self._value: dict[str, object] = {
            "profile_resolver_state": "startup",
            "profile_last_detection_at": "",
            "profile_candidate_id": "",
            "profile_evidence_markers": [],
            "profile_evidence_strengths": [],
            "profile_resolution_status": "",
            "profile_resolution_reason": "",
            "profile_resolution_transition": "",
            "profile_activation_decision": "",
            "profile_resolution_latency_ms": None,
            "profile_resolution_retry_count": 0,
            "profile_resolution_window_generation": 0,
            "profile_resolution_registry_generation": 0,
        }

    def replace(self, **fields: object) -> None:
        with self._lock:
            self._value = {
                "profile_resolver_state": fields.get("resolver_state", ""),
                "profile_last_detection_at": fields.get("last_detection_at", ""),
                "profile_candidate_id": fields.get("candidate_profile_id", ""),
                "profile_evidence_markers": list(fields.get("matched_markers", []) or []),
                "profile_evidence_strengths": list(fields.get("marker_strengths", []) or []),
                "profile_resolution_status": fields.get("status", ""),
                "profile_resolution_reason": fields.get("reason", ""),
                "profile_resolution_transition": fields.get("state_transition", ""),
                "profile_activation_decision": fields.get("activation_decision", ""),
                "profile_resolution_latency_ms": fields.get("latency_ms"),
                "profile_resolution_retry_count": fields.get("schema_retry_count", 0),
                "profile_resolution_window_generation": fields.get("window_generation", 0),
                "profile_resolution_registry_generation": fields.get("registry_generation", 0),
            }

    def current(self) -> dict[str, object]:
        with self._lock:
            return dict(self._value)


profile_resolution_status = ProfileResolutionStatusStore()
