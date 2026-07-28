"""Bounded, metadata-only activity context shared by STT and translation.

Translation requests bind one immutable snapshot in a ``ContextVar``.  This
keeps the prompt, engine-specific prompt/cache signature, API request, and
runtime metadata on the same activity even if the manual dashboard value
changes while a request is in flight.
"""

from __future__ import annotations

import contextvars
import hashlib
import math
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator


MAX_ACTIVITY_CHARS = 80
ACTIVITY_CONTEXT_SCHEMA_VERSION = 2
AUTOMATIC_ACTIVITY_KINDS = frozenset(
    {
        "game",
        "application",
        "media",
        "chatting",
        "singing",
        "music",
        "creative",
        "other",
    }
)
_UNSAFE_INSTRUCTION_RE = re.compile(
    r"(?:"
    r"\b(?:ignore|disregard|forget|override|follow|obey|reveal|show|print|repeat|translate)\b"
    r".{0,48}\b(?:instruction|prompt|message|rule)s?\b"
    r"|"
    r"\b(?:ignore|disregard|forget|override|follow|obey|reveal|show|print|repeat|translate)\b"
    r".{0,48}\b(?:system|developer|assistant|text)\b"
    r"|"
    r"\b(?:system|developer|assistant)\s*:"
    r")",
    re.IGNORECASE,
)
_ACTIVITY_ALIASES = {
    "pokemon": "pokemon",
    "pokémon": "pokemon",
    "pocket monsters": "pokemon",
    "포켓몬": "pokemon",
    "minecraft": "minecraft",
    "마인크래프트": "minecraft",
    "starcraft": "starcraft",
    "starcraft ii": "starcraft",
    "스타크래프트": "starcraft",
    "hades": "hades",
    "하데스": "hades",
    "league of legends": "league_of_legends",
    "lol": "league_of_legends",
    "리그 오브 레전드": "league_of_legends",
    "리그오브레전드": "league_of_legends",
}
_CANONICAL_ACTIVITY_LABELS = {
    "pokemon": "Pokémon",
    "minecraft": "Minecraft",
    "starcraft": "StarCraft",
    "hades": "Hades",
    "league_of_legends": "League of Legends",
}


@dataclass(frozen=True)
class ActivitySnapshot:
    """One request's stable activity identity and display value."""

    activity_id: str
    display_label: str
    source: str
    schema_version: int
    captured_at_utc: str
    activity_kind: str = ""

    @property
    def cache_identity(self) -> str:
        return f"activity-v{self.schema_version}:{self.activity_id}"


@dataclass(frozen=True)
class AutomaticActivityPublication:
    """One bounded automatic activity candidate shared across threads."""

    activity_id: str
    display_label: str
    confirmed_at_utc: str
    fresh_until_monotonic: float
    confidence: float
    evidence_count: int
    activity_kind: str


class ActivityPublicationStore:
    """Thread-safe bridge from the scene resolver to translation requests.

    The store never reads or writes ``cfg.translation.current_activity``.
    Callers must pass the current manual value when capturing a request so
    manual precedence remains explicit and testable.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._automatic: AutomaticActivityPublication | None = None

    def replace(
        self,
        publication: AutomaticActivityPublication | None,
    ) -> bool:
        """Replace the bounded automatic candidate; return whether it changed."""
        if publication is not None:
            expected_id, expected_label, expected_kind = (
                automatic_activity_identity(
                    publication.display_label,
                    kind=publication.activity_kind,
                )
            )
            if (
                not expected_id
                or publication.activity_id != expected_id
                or publication.display_label != expected_label
                or publication.activity_kind != expected_kind
                or isinstance(publication.fresh_until_monotonic, bool)
                or not isinstance(
                    publication.fresh_until_monotonic,
                    (int, float),
                )
                or not math.isfinite(publication.fresh_until_monotonic)
                or isinstance(publication.confidence, bool)
                or not isinstance(publication.confidence, (int, float))
                or not math.isfinite(publication.confidence)
                or not 0.0 <= publication.confidence <= 1.0
                or isinstance(publication.evidence_count, bool)
                or not isinstance(publication.evidence_count, int)
                or not 2 <= publication.evidence_count <= 100
            ):
                raise ValueError(
                    "automatic activity publication identity must match its label"
                )
        with self._lock:
            changed = publication != self._automatic
            self._automatic = publication
            return changed

    def current(self) -> AutomaticActivityPublication | None:
        """Return only a still-fresh automatic candidate."""
        with self._lock:
            publication = self._automatic
            if (
                publication is not None
                and self._clock() >= publication.fresh_until_monotonic
            ):
                self._automatic = None
                return None
            return publication

    def capture(
        self,
        manual_value: object,
        *,
        automatic_enabled: bool,
    ) -> ActivitySnapshot:
        """Capture manual > fresh automatic > empty for one translation."""
        manual = normalize_activity(manual_value)
        if manual:
            return capture_activity_snapshot(manual, source="manual")
        publication = self.current() if automatic_enabled else None
        if publication is None:
            return capture_activity_snapshot("", source="none")
        return ActivitySnapshot(
            activity_id=publication.activity_id,
            display_label=publication.display_label,
            source="automatic",
            schema_version=ACTIVITY_CONTEXT_SCHEMA_VERSION,
            captured_at_utc=datetime.now(timezone.utc).isoformat(),
            activity_kind=publication.activity_kind,
        )


_BOUND_ACTIVITY: contextvars.ContextVar[ActivitySnapshot | None] = (
    contextvars.ContextVar("live_translate_activity_snapshot", default=None)
)
activity_publication_store = ActivityPublicationStore()


def normalize_activity(value: object, *, max_chars: int = MAX_ACTIVITY_CHARS) -> str:
    """Return one short line suitable for matching, logs, and prompt metadata."""
    if not isinstance(value, str):
        return ""
    # Reject controls, invisible format characters, surrogates, private-use,
    # and other Unicode "Other" categories before whitespace folding. This is
    # fail-closed metadata, not source speech, so preserving such characters
    # has no legitimate benefit and can hide prompt-like instructions.
    if any(unicodedata.category(char).startswith("C") for char in value):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        return ""
    one_line = " ".join(normalized.split())
    if not one_line or max_chars <= 0:
        return ""
    if _UNSAFE_INSTRUCTION_RE.search(one_line):
        return ""
    return one_line[:max_chars].rstrip()


def activity_id_for_label(value: object) -> str:
    """Return a stable, bounded identity without exposing the raw label.

    The small registry deliberately covers only activities already supported
    by this project.  Unknown manual labels get a deterministic normalized
    identifier so existing user-entered context remains usable without
    expanding an automatic-classification taxonomy.
    """
    label = normalize_activity(value)
    if not label:
        return ""
    folded = label.casefold()
    if folded in _ACTIVITY_ALIASES:
        return _ACTIVITY_ALIASES[folded]
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if slug:
        return slug[:64]
    # Non-Latin manual labels are allowed, but the identity remains bounded
    # and is never used as prompt text.
    return _manual_activity_id(folded)


def automatic_activity_identity(
    value: object,
    *,
    kind: object,
) -> tuple[str, str, str]:
    """Return a one-to-one bounded identity for an accepted automatic label.

    Reviewed aliases retain their historical IDs. Open-set labels use an
    opaque digest of the complete normalized kind/label pair; no lossy slug is
    used as cache identity.
    """
    label = normalize_activity(value, max_chars=MAX_ACTIVITY_CHARS + 1)
    normalized_kind = (
        str(kind or "").strip().casefold()
        if isinstance(kind, str)
        else ""
    )
    if (
        not label
        or len(label) > MAX_ACTIVITY_CHARS
        or normalized_kind not in AUTOMATIC_ACTIVITY_KINDS
    ):
        return "", "", ""
    alias_id = _ACTIVITY_ALIASES.get(label.casefold(), "")
    if alias_id:
        return alias_id, _CANONICAL_ACTIVITY_LABELS[alias_id], "game"
    digest_input = f"{normalized_kind}\0{label.casefold()}".encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()[:20]
    return f"auto-{digest}", label, normalized_kind


def _manual_activity_id(normalized_label: str) -> str:
    digest = hashlib.sha256(normalized_label.encode("utf-8")).hexdigest()[:16]
    return "manual-" + digest


def capture_activity_snapshot(
    value: object,
    *,
    source: str = "manual",
) -> ActivitySnapshot:
    label = normalize_activity(value)
    normalized_source = str(source or "manual")
    activity_id = activity_id_for_label(label)
    canonical_label = _CANONICAL_ACTIVITY_LABELS.get(activity_id, "")
    if normalized_source == "manual":
        # Manual display text must stay byte-for-byte compatible with T12.
        # When it is not the one deterministic label for a canonical id, give
        # it a label-specific id so a future id-keyed cache cannot collapse
        # two different prompt capsules.
        if label and label != canonical_label:
            activity_id = _manual_activity_id(label.casefold())
    elif canonical_label:
        label = canonical_label
    return ActivitySnapshot(
        activity_id=activity_id,
        display_label=label,
        source=normalized_source,
        schema_version=ACTIVITY_CONTEXT_SCHEMA_VERSION,
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
        activity_kind="",
    )


def capture_effective_activity_snapshot(
    manual_value: object,
    *,
    automatic_enabled: bool,
    publication_store: ActivityPublicationStore = activity_publication_store,
) -> ActivitySnapshot:
    """Capture the effective activity without mutating global configuration."""
    return publication_store.capture(
        manual_value,
        automatic_enabled=automatic_enabled,
    )


def bound_activity_snapshot() -> ActivitySnapshot | None:
    return _BOUND_ACTIVITY.get()


def effective_activity_value(fallback: object = "") -> str:
    snapshot = bound_activity_snapshot()
    if snapshot is not None:
        return snapshot.display_label
    return normalize_activity(fallback)


@contextmanager
def bind_activity_snapshot(snapshot: ActivitySnapshot) -> Iterator[ActivitySnapshot]:
    token = _BOUND_ACTIVITY.set(snapshot)
    try:
        yield snapshot
    finally:
        _BOUND_ACTIVITY.reset(token)


def activity_prompt_capsule(value: object) -> str:
    """Build the sole activity capsule; the value is context, never source."""
    activity = normalize_activity(value)
    if not activity:
        return ""
    return (
        f"[Background] Current stream activity: {activity}\n"
        "Use this metadata only to disambiguate game/context-specific terms. "
        "Never translate, mention, or copy it into the output."
    )
