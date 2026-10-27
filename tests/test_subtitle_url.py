"""scripts/subtitle_url.py and the offline lyrics style (no network: yt-dlp and the job are stubbed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import subtitle_url  # noqa: E402

from modules import offline_subtitles  # noqa: E402


def test_safe_name_is_windows_safe():
    assert subtitle_url.safe_name('【 MV 】 WISHes(위시스) - I MY ME MINE') == "【 MV 】 WISHes(위시스) - I MY ME MINE"
    assert subtitle_url.safe_name('a/b\\c:d*e?f"g<h>i|j.') == "a b c d e f g h i j"
    assert subtitle_url.safe_name("...") == "video"
    assert subtitle_url.safe_name("CON") == "_CON"
    assert subtitle_url.safe_name("nul.mv") == "_nul.mv"
    assert len(subtitle_url.safe_name("가" * 200)) == 80


def test_plan_prefers_the_uploaders_korean_track_over_audio():
    assert subtitle_url.plan({"subtitles": {"ko": [{"ext": "vtt"}]}}, False) == ("translate", "ko")
    assert subtitle_url.plan({"subtitles": {"ko-KR": [{}]}}, False) == ("translate", "ko-KR")
    # auto-generated captions live under automatic_captions and are not used
    assert subtitle_url.plan({"subtitles": {}, "automatic_captions": {"ko": [{}]}}, False) == ("transcribe", None)
    assert subtitle_url.plan({"subtitles": {"en": [{}]}}, False) == ("transcribe", None)
    assert subtitle_url.plan({"subtitles": {"ko": [{}]}}, True) == ("transcribe", None)


def test_live_streams_and_playlists_are_refused():
    for info in ({"_type": "playlist"}, {"is_live": True}, {"live_status": "is_upcoming"}):
        with pytest.raises(subtitle_url.ToolError):
            subtitle_url.check_video(info)
    subtitle_url.check_video({"_type": "video", "live_status": "was_live"})


def test_long_transcription_needs_confirmation(monkeypatch):
    subtitle_url.confirm_long_transcribe(20 * 60, False)          # short: no question
    subtitle_url.confirm_long_transcribe(3 * 3600, True)          # --yes
    monkeypatch.setattr(sys, "stdin", None)
    with pytest.raises(subtitle_url.ToolError, match="--yes"):
        subtitle_url.confirm_long_transcribe(3 * 3600, False)
    with pytest.raises(subtitle_url.ToolError, match="長度不明"):
        subtitle_url.confirm_long_transcribe(0, False)       # unknown length is not "short"


def test_confirmation_answer_on_a_terminal(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    subtitle_url.confirm_long_transcribe(3 * 3600, False)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(subtitle_url.ToolError, match="已取消"):
        subtitle_url.confirm_long_transcribe(3 * 3600, False)


class _FakeYDL:
    """Writes the files yt-dlp would write for the configured outtmpl."""

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        template = self.options["outtmpl"]
        if self.options.get("writesubtitles"):
            Path(f"{template}.{self.options['subtitleslangs'][0]}.vtt").write_text("WEBVTT\n", encoding="utf-8")
        else:
            Path(template.replace("%(ext)s", "m4a")).write_bytes(b"")


def _run(monkeypatch, tmp_path, info, argv):
    calls = {}
    monkeypatch.setattr(subtitle_url, "fetch_info", lambda url: info)
    monkeypatch.setattr(subtitle_url, "_ydl", _FakeYDL)
    import make_subtitles

    def fake_job(job):
        calls["job"] = job
        calls["input_name"] = Path(job[2]).name
        return 0

    monkeypatch.setattr(make_subtitles, "main", fake_job)
    calls["status"] = subtitle_url.main([*argv, "--out", str(tmp_path), "--no-open"])
    return calls


def test_bracketed_title_with_a_subtitle_track_translates_with_lyrics(monkeypatch, tmp_path):
    # "[MV] ..." used to be read as a glob pattern and the downloaded file was not found
    info = {"title": "[MV] 곡 제목", "id": "abc123", "subtitles": {"ko": [{}]}}
    calls = _run(monkeypatch, tmp_path, info, ["https://youtu.be/abc123", "--lyrics"])
    job = calls["job"]
    assert calls["status"] == 0
    assert job[0] == "translate" and calls["input_name"] == "[MV] 곡 제목.vtt"
    assert job[job.index("--out-dir") + 1] == str(tmp_path / "[MV] 곡 제목 [abc123]")
    assert "--lyrics" in job


def test_no_track_transcribes_the_audio(monkeypatch, tmp_path):
    info = {"title": "[LIVE] 방송", "id": "xyz", "subtitles": {}, "duration": 600}
    calls = _run(monkeypatch, tmp_path, info, ["https://youtu.be/xyz"])
    assert calls["status"] == 0
    assert calls["job"][0] == "transcribe" and calls["input_name"] == "[LIVE] 방송.m4a"
    assert "--lyrics" not in calls["job"]


def test_rerun_is_refused_before_any_download_or_paid_call(monkeypatch, tmp_path, capsys):
    done = tmp_path / "곡 [abc]"
    done.mkdir()
    (done / "곡.zh-TW.srt").write_text("", encoding="utf-8")
    info = {"title": "곡", "id": "abc", "subtitles": {"ko": [{}]}}
    calls = _run(monkeypatch, tmp_path, info, ["https://youtu.be/abc"])
    assert calls["status"] == 1 and "job" not in calls
    assert "已經做過了" in capsys.readouterr().err


def test_lyrics_style_only_changes_the_system_prompt():
    rows, facts = [{"id": "1", "ko": "가사"}], "FACTS"
    plain = offline_subtitles.messages_for(rows, facts, [])
    lyrics = offline_subtitles.messages_for(rows, facts, [], "lyrics")
    assert plain[1] == lyrics[1]
    assert lyrics[0][1] == plain[0][1] + "\n" + offline_subtitles.STYLE_INSTRUCTIONS["lyrics"]


def test_unknown_style_is_rejected():
    with pytest.raises(ValueError):
        offline_subtitles.translate([], None, None, lambda *a, **k: None, style="poem")
