from dataclasses import replace

from modules.activity_context import capture_activity_snapshot
from modules.session_context import LiveSessionSnapshot


def test_live_session_owns_history_namespace_and_activity_epoch():
    session = LiveSessionSnapshot("session-a")
    activity = replace(
        capture_activity_snapshot("League of Legends", source="manual"),
        cohort_epoch=4,
    )

    assert session.history_cohort(activity) == (
        "session-a",
        "league_of_legends",
        4,
    )


def test_live_sessions_do_not_share_history_namespace():
    activity = capture_activity_snapshot("chatting", source="manual")

    assert (
        LiveSessionSnapshot("session-a").history_cohort(activity)
        != LiveSessionSnapshot("session-b").history_cohort(activity)
    )
