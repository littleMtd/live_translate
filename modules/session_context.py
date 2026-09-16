"""Live-session ownership for conversation continuity."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from modules.activity_context import ActivitySnapshot


HistoryCohort = tuple[str, str, int]
DEFAULT_HISTORY_COHORT: HistoryCohort = ("default", "unknown", 0)


@dataclass(frozen=True)
class LiveSessionSnapshot:
    """Immutable identity of one process-local conversation session."""

    session_id: str

    @classmethod
    def create(cls) -> "LiveSessionSnapshot":
        return cls(uuid.uuid4().hex[:12])

    def history_cohort(
        self, activity: ActivitySnapshot | None
    ) -> HistoryCohort:
        return (
            self.session_id,
            (activity.activity_id if activity is not None else "") or "unknown",
            max(0, int(activity.cohort_epoch or 0)) if activity is not None else 0,
        )
