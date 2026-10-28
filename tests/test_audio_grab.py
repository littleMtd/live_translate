"""scripts/audio_grab.py and its window (no network, no audio device, no child process)."""
import sys
import threading
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audio_grab  # noqa: E402
import audio_grab_gui  # noqa: E402


def test_names_never_overwrite(tmp_path):
    assert audio_grab.unique_path(tmp_path, "a", ".flac") == tmp_path / "a.flac"
    (tmp_path / "a.flac").write_bytes(b"")
    (tmp_path / "a (2).flac").write_bytes(b"")
    assert audio_grab.unique_path(tmp_path, "a", ".flac") == tmp_path / "a (3).flac"


def test_live_detection_and_stream_choice():
    assert audio_grab.is_live({"is_live": True}) and audio_grab.is_live({"live_status": "is_live"})
    assert not audio_grab.is_live({"live_status": "was_live"})
    video = {"format_id": "v", "acodec": "none", "url": "v"}
    audio = {"format_id": "a", "acodec": "opus", "url": "a"}
    assert audio_grab.stream_of({"requested_formats": [video, audio]}) is audio
    muxed = {"url": "m3u8", "acodec": "mp4a.40.2"}
    assert audio_grab.stream_of(muxed) is muxed


def test_live_command_copies_audio_only_with_site_headers(monkeypatch, tmp_path):
    monkeypatch.setattr(audio_grab, "ffmpeg_path", lambda: "ffmpeg")
    command = audio_grab.live_command({"url": "https://x/live.m3u8", "http_headers": {"Referer": "https://x"}},
                                      tmp_path / "a.aac", "adts")
    assert command[command.index("-headers") + 1] == "Referer: https://x\r\n"
    assert command[command.index("-i") + 1] == "https://x/live.m3u8"
    assert ["-vn", "-c:a", "copy", "-f", "adts"] == command[-6:-1]      # no picture, no re-encode
    assert "-headers" not in audio_grab.live_command({"url": "u"}, tmp_path / "a.mka", "matroska")


@pytest.mark.parametrize("info, message", [
    ({"_type": "playlist"}, "播放清單"),
    ({"live_status": "is_upcoming"}, "還沒開始"),
])
def test_unusable_urls_are_refused(monkeypatch, info, message):
    class FakeYDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            return info

    monkeypatch.setattr(audio_grab, "_ydl", FakeYDL)
    with pytest.raises(audio_grab.ToolError, match=message):
        audio_grab.fetch_info("https://x")


class _Device:
    def __init__(self, name, ident):
        self.name, self.id = name, ident


def test_device_choice_prefers_exact_names_and_kind(monkeypatch):
    speakers = [_Device("Realtek Digital Output(Optical)", 1), _Device("Realtek Digital Output", 2),
                _Device("Headset (WH-1000XM4 Hands-Free)", 3)]
    inputs = [_Device("Headset (WH-1000XM4 Hands-Free)", 4)]
    fake = type("SC", (), {
        "all_speakers": staticmethod(lambda: speakers),
        "all_microphones": staticmethod(lambda include_loopback=False: inputs),
        "get_microphone": staticmethod(lambda ident, include_loopback=False: ("loopback", ident)),
        "default_speaker": staticmethod(lambda: speakers[0]),
    })
    monkeypatch.setattr(audio_grab, "_soundcard", lambda: fake)
    assert audio_grab.pick_device("Realtek Digital Output")[1] == ("loopback", 2)       # exact beats partial
    assert audio_grab.pick_device("optical")[1] == ("loopback", 1)                      # partial still works
    headset = "Headset (WH-1000XM4 Hands-Free)"
    assert audio_grab.pick_device(headset, "input")[1] is inputs[0]                      # same name, the input
    assert audio_grab.pick_device(headset, "speaker")[1] == ("loopback", 3)
    with pytest.raises(audio_grab.ToolError):
        audio_grab.pick_device("nothing like this")


def test_stopping_a_video_download_is_a_clean_message(monkeypatch, tmp_path):
    class CancelledYDL:
        def __init__(self, options):
            self.hooks = options["progress_hooks"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            for hook in self.hooks:
                hook({"status": "downloading", "downloaded_bytes": 1})   # raises: stop is set
            raise AssertionError("hook did not cancel")

    monkeypatch.setattr(audio_grab, "_ydl", CancelledYDL)
    monkeypatch.setattr(audio_grab, "ffmpeg_path", lambda: "ffmpeg")
    stop = threading.Event()
    stop.set()
    with pytest.raises(audio_grab.ToolError, match="已停止"):
        audio_grab.grab_vod("https://x", {"title": "t", "id": "i"}, tmp_path, stop)


def test_live_recording_survives_a_noisy_ffmpeg(monkeypatch, tmp_path):
    """ffmpeg writing lots of errors must not stall the recording (stderr is drained)."""
    script = tmp_path / "noisy_ffmpeg.py"
    script.write_text(
        "import sys, time\n"
        "out = sys.argv[-1]\n"
        "open(out, 'wb').write(b'x')\n"
        "for _ in range(4000): sys.stderr.write('Failed to open segment ' + 'x' * 40 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdin.read(1)\n", encoding="utf-8")
    monkeypatch.setattr(audio_grab, "live_command",
                        lambda stream, target, container: [sys.executable, str(script), str(target)])
    stop = threading.Event()
    threading.Timer(1.5, stop.set).start()
    started = audio_grab.time.monotonic()
    path = audio_grab.grab_live({"url": "u", "acodec": "mp4a.40.2", "title": "t"}, tmp_path, stop)
    assert path.suffix == ".aac" and path.stat().st_size == 1
    assert audio_grab.time.monotonic() - started < 10          # "q" was read, no 15 s kill


def test_stop_file_stops_without_reading_stdin(tmp_path):
    stop, flag = threading.Event(), tmp_path / "x.stop"
    watcher = threading.Thread(target=audio_grab._watch_stop_file, args=(flag, stop), daemon=True)
    watcher.start()
    assert not stop.wait(0.3)
    flag.write_text("", encoding="utf-8")
    assert stop.wait(2.0)


def test_window_commands_and_progress_text():
    command = audio_grab_gui.build_command("url", url="https://x", out="D:/a", stop_file="s")
    assert command[1:4] == [str(audio_grab_gui.TOOL), "url", "https://x"]
    assert command[-4:] == ["--out", "D:/a", "--stop-file", "s"]
    record = audio_grab_gui.build_command("record", device="CABLE Output", kind="input", out="o", stop_file="s")
    assert record[2:5] == ["record", "--device=CABLE Output", "--kind=input"]
    assert not any(a.startswith("--device") for a in
                   audio_grab_gui.build_command("record", out="o", stop_file="s"))
    describe = audio_grab_gui.describe
    assert describe('{"stage": "download", "done": 1000000, "total": 4000000}')[:2] == ("下載中 1.0 / 4.0 MB", 0.25)
    assert describe('{"stage": "live", "seconds": 3725, "bytes": 2500000}')[0] == "直播錄音中 1:02:05（2.5 MB）"
    assert describe('{"stage": "record", "seconds": 65, "level": 0.0}') == ("錄音中 1:05（沒有聲音）", None, 0.0, None)
    assert describe("完成：C:/a/b.flac") == ("完成：C:/a/b.flac", 1.0, None, "C:/a/b.flac")
    assert audio_grab_gui.device_label({"name": "耳機", "kind": "speaker", "default": True}) == "耳機（你現在聽到的聲音）"
    assert audio_grab_gui.device_label({"name": "CABLE Output", "kind": "input", "default": False}) == "CABLE Output（輸入）"


def test_window_builds_and_lists_devices(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    monkeypatch.setattr(root, "clipboard_get", lambda: "")
    monkeypatch.setattr(audio_grab_gui.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: None})())
    try:
        app = audio_grab_gui.App(root)
        app._on_devices([{"name": "耳機", "kind": "speaker", "default": True},
                         {"name": "CABLE Output", "kind": "input", "default": False}])
        assert list(app.device.cget("values")) == ["耳機（你現在聽到的聲音）", "CABLE Output（輸入）"]
        assert str(app.device.cget("state")) == "disabled"          # URL mode by default
        app.mode.set("record")
        app._mode_changed()
        assert str(app.device.cget("state")) == "readonly"
    finally:
        root.destroy()
