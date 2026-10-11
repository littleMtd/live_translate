from modules.live_status import LiveStatus


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_all_quiet_has_no_flags():
    status = LiveStatus(clock=Clock())
    status.record_stt_engine("elevenlabs")
    status.record_translation(True)
    assert status.flags(profile_unresolved=False) == []


def test_unresolved_auto_profile_flags_general():
    assert LiveStatus(clock=Clock()).flags(profile_unresolved=True) == ["general"]


def test_groq_fallback_flag_shows_only_while_recent():
    clock = Clock()
    status = LiveStatus(clock=clock)
    status.record_stt_engine("groq")
    assert status.flags(profile_unresolved=False) == ["Groq 辨識"]
    clock.now += 31
    assert status.flags(profile_unresolved=False) == []
    status.record_stt_engine("elevenlabs")
    assert status.flags(profile_unresolved=False) == []


def test_translation_failing_needs_three_recent_failures_in_a_row():
    clock = Clock()
    status = LiveStatus(clock=clock)
    status.record_translation(False)
    status.record_translation(False)
    assert status.flags(profile_unresolved=False) == []
    status.record_translation(False)
    assert status.flags(profile_unresolved=False) == ["翻譯失敗"]
    status.record_translation(True)  # one success clears it
    assert status.flags(profile_unresolved=False) == []
    for _ in range(3):
        status.record_translation(False)
    clock.now += 91  # the streak is old: no longer "now"
    assert status.flags(profile_unresolved=False) == []


def test_history_keeps_recent_subtitles_with_source_and_skips_empty():
    status = LiveStatus(clock=Clock())
    for i in range(25):
        status.record_subtitle(f"원문 {i}", f"譯文 {i}")
    status.record_subtitle("원문", "")
    recent = status.recent_subtitles(3)
    assert [(r.source, r.target) for r in recent] == [("원문 22", "譯文 22"), ("원문 23", "譯文 23"), ("원문 24", "譯文 24")]
    assert status.recent_subtitles(0) == []
