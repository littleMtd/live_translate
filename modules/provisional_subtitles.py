"""Provider-neutral contracts for one-shot provisional subtitles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
from typing import Any

from modules.activity_context import ActivitySnapshot
from modules.profile_context import ProfileSnapshot


@dataclass(frozen=True)
class ProvisionalRequest:
    provisional_id: str
    text: str
    incomplete: bool
    profile_id: str
    source_utterance_ids: tuple[str, ...]
    evidence_source_utterance_ids: tuple[str, ...]
    activity_snapshot: ActivitySnapshot
    requested_at_monotonic: float
    first_stt_ready_at_monotonic: float
    min_avg_logprob: float | None = None
    max_no_speech_prob: float | None = None
    cut_reason: str = ""
    forced: bool = False
    profile_snapshot: ProfileSnapshot | None = None


@dataclass(frozen=True)
class ProvisionalCandidate:
    provisional_id: str
    raw_target: str
    display_target: str
    fingerprint: str
    engine: str
    model: str
    requested_at_monotonic: float
    completed_at_monotonic: float
    usage: dict[str, Any]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class SubtitlePayload:
    text: str
    subtitle_id: str
    revision: int = 0
    phase: str = "final"


def provisional_fingerprint(
    *,
    prepared_source: str,
    source_utterance_ids: tuple[str, ...],
    evidence_source_utterance_ids: tuple[str, ...],
    profile_id: str,
    activity_cache_identity: str,
    history_cohort: tuple[str, str, int],
    messages: tuple[tuple[str, str], ...],
    incomplete: bool,
    profile_cache_identity: str = "",
) -> str:
    payload = {
        "source": prepared_source,
        "source_ids": list(source_utterance_ids),
        "evidence_ids": list(evidence_source_utterance_ids),
        "profile": profile_id,
        "profile_cache_identity": profile_cache_identity,
        "activity": activity_cache_identity,
        "history_cohort": list(history_cohort),
        "messages": [list(message) for message in messages],
        "incomplete": bool(incomplete),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProvisionalStore:
    """Small session-local candidate store; never persists conversation state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._candidates: dict[str, ProvisionalCandidate] = {}
        self._closed: set[str] = set()

    def publish(self, candidate: ProvisionalCandidate) -> bool:
        with self._lock:
            if candidate.provisional_id in self._closed:
                return False
            self._candidates[candidate.provisional_id] = candidate
            return True

    def candidate(self, provisional_id: str) -> ProvisionalCandidate | None:
        with self._lock:
            return self._candidates.get(provisional_id)

    def close(self, provisional_id: str) -> ProvisionalCandidate | None:
        if not provisional_id:
            return None
        with self._lock:
            self._closed.add(provisional_id)
            return self._candidates.pop(provisional_id, None)

    def is_closed(self, provisional_id: str) -> bool:
        with self._lock:
            return provisional_id in self._closed
