"""scripts/subtitle_gui.py helpers and queue flow (hidden window, fake child process)."""
import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import subtitle_gui  # noqa: E402


def opt(command, name):
    """Value of a "--name=value" argument, or None."""
    return next((a.split("=", 1)[1] for a in command if a.startswith(name + "=")), None)


def test_command_carries_every_option():
    command = subtitle_gui.build_command("https://youtu.be/x", lyrics=True, profile="stellive_hina",
                                         out="D:/subs", prefer_audio=True, assume_yes=True)
    assert command[1:4] == [str(subtitle_gui.TOOL), "https://youtu.be/x", "--no-open"]
    assert command[command.index("--out") + 1] == "D:/subs"
    assert command[command.index("--profile") + 1] == "stellive_hina"
    assert {"--lyrics", "--prefer-audio", "--yes"} <= set(command)
    plain = subtitle_gui.build_command("u", lyrics=False, profile="", out="o", prefer_audio=False,
                                       assume_yes=False)
    assert not {"--lyrics", "--prefer-audio", "--yes", "--profile"} & set(plain)
    assert opt(plain, "--background") is None
    noted = subtitle_gui.build_command("u", lyrics=True, profile="", out="o", prefer_audio=False,
                                       assume_yes=False, background="  兩人合唱\n藍色是姊姊  ")
    assert opt(noted, "--background") == "兩人合唱\n藍色是姊姊"
    blank = subtitle_gui.build_command("u", lyrics=True, profile="", out="o", prefer_audio=False,
                                       assume_yes=False, background=" \n ")
    assert opt(blank, "--background") is None
    # a note starting with "-" stays a value
    dashed = subtitle_gui.build_command("u", lyrics=True, profile="", out="o", prefer_audio=False,
                                        assume_yes=False, background="-member", singers="-A")
    assert opt(dashed, "--background") == "-member" and opt(dashed, "--singers") == "-A"


def test_progress_lines_are_described():
    assert subtitle_gui.describe('{"stage": "translate", "done": 20, "total": 80}') == ("翻譯 20/80", 0.25, None)
    assert subtitle_gui.describe('{"stage": "transcribe", "done": 1, "total": 2}')[1] == 0.5
    assert subtitle_gui.describe('{"stage": "error", "message": "boom"}')[0] == "[失敗] boom"
    assert subtitle_gui.describe("完成：C:/out/x") == ("完成：C:/out/x", 1.0, "C:/out/x")
    assert subtitle_gui.describe("讀取影片資訊…") == ("讀取影片資訊…", None, None)
    assert subtitle_gui.describe('{"stage": "done", "files": {}}') == (None, None, None)
    assert subtitle_gui.describe("{not json") == (None, None, None)


def test_already_done_message_points_at_the_existing_folder():
    line = "[失敗] 這支影片已經做過了：C:\\字幕\\歌 [abc]（要重做請先刪掉或改 --out）"
    assert subtitle_gui.finished_folder(line) == "C:\\字幕\\歌 [abc]"
    assert subtitle_gui.finished_folder("[失敗] 音訊下載失敗") is None
    # a full-width （ in the title itself
    title_line = "[失敗] 這支影片已經做過了：C:\\字幕\\歌（Live） [abc]（要重做請先刪掉或改 --out）"
    assert subtitle_gui.finished_folder(title_line) == "C:\\字幕\\歌（Live） [abc]"


def test_child_runs_on_console_python_so_ffmpeg_windows_stay_hidden(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(subtitle_gui.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert subtitle_gui.console_python() == str(tmp_path / "python.exe")
    monkeypatch.setattr(subtitle_gui.sys, "executable", str(tmp_path / "python.exe"))
    assert subtitle_gui.console_python() == str(tmp_path / "python.exe")


def test_profile_list_survives_a_missing_profile_file(monkeypatch, tmp_path):
    monkeypatch.setattr(subtitle_gui, "ROOT", tmp_path)
    assert subtitle_gui.profile_choices() == [("不指定", "")]


def test_profile_dropdown_starts_with_none_and_lists_real_profiles():
    choices = subtitle_gui.profile_choices()
    assert choices[0] == ("不指定", "")
    assert ("Stellive Hina", "stellive_hina") in choices


def test_new_options_reach_the_command():
    command = subtitle_gui.build_command("u", lyrics=False, profile="", out="o", prefer_audio=False,
                                         assume_yes=False, singers=" A, B ", translator="deepseek",
                                         retranslate=True)
    assert opt(command, "--singers") == "A, B"
    assert command[command.index("--translator") + 1] == "deepseek"
    assert "--retranslate" in command
    default = subtitle_gui.build_command("u", lyrics=False, profile="", out="o", prefer_audio=False,
                                         assume_yes=False)
    assert not {"--translator", "--retranslate"} & set(default)   # gemini is the tool default
    assert opt(default, "--singers") is None


def test_urls_are_found_in_order_without_repeats():
    text = "https://youtu.be/a\nhttps://youtu.be/b  https://youtu.be/a 不是網址"
    assert subtitle_gui.parse_urls(text) == ["https://youtu.be/a", "https://youtu.be/b"]
    assert subtitle_gui.parse_urls("") == []


def test_info_line_is_picked_from_the_output():
    out = '讀取…\n{"info": {"title": "t", "description": "d"}}\n'
    assert subtitle_gui.parse_info(out) == {"title": "t", "description": "d"}
    assert subtitle_gui.parse_info("[失敗] x") is None


@pytest.fixture
def app(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    monkeypatch.setattr(root, "clipboard_get", lambda: "")
    launched = []
    instance = subtitle_gui.App(root)

    class FakeProcess:
        def __init__(self, command, **kwargs):
            launched.append(command)
            self.stdout = []

        def wait(self):
            return 0

    monkeypatch.setattr(subtitle_gui.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(subtitle_gui.threading, "Thread", lambda **kw: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(instance, "_open_folder", lambda: None)
    instance.launched = launched
    yield instance
    root.destroy()


def test_queue_runs_each_url_and_offers_retranslation(app, monkeypatch):
    answers = iter([True])
    monkeypatch.setattr(subtitle_gui.messagebox, "askyesno", lambda *a: next(answers))
    app.url.set("https://youtu.be/a https://youtu.be/b")
    app.singers.set("A,B")
    app.start()
    assert app.launched[-1][2] == "https://youtu.be/a"
    assert opt(app.launched[-1], "--singers") == "A,B"
    app._on_line("完成：C:/out/a")
    app._on_exit(0)
    assert app.launched[-1][2] == "https://youtu.be/b" and "--retranslate" not in app.launched[-1]
    app._on_line("[失敗] 這支影片已經做過了：C:/out/b（要重做請先刪掉或改 --out）")
    app._on_exit(1)                                   # owner says yes: same URL, retranslated
    assert app.launched[-1][2] == "https://youtu.be/b" and "--retranslate" in app.launched[-1]
    app._on_line("完成：C:/out/b")
    app._on_exit(0)
    assert len(app.launched) == 3 and app.succeeded == ["C:/out/a", "C:/out/b"] and app.failed == []
    assert str(app.start_button.cget("state")) == "normal"
    assert "全部結束：完成 2 支，失敗 0 支" in app.log.get("1.0", "end")


def test_a_failed_url_does_not_stop_the_queue(app):
    app.url.set("https://youtu.be/a https://youtu.be/b")
    app.start()
    app._on_line('{"stage": "error", "message": "boom"}')
    app._on_exit(1)
    assert app.launched[-1][2] == "https://youtu.be/b"
    app._on_line("完成：C:/out/b")
    app._on_exit(0)
    assert app.failed == ["https://youtu.be/a"] and app.output_dir == "C:/out/b"


def test_description_fills_the_background_box(app, monkeypatch):
    monkeypatch.setattr(subtitle_gui.messagebox, "askyesno", lambda *a: False)
    app._on_info({"title": "t", "duration": 125, "korean_track": True, "done": False, "description": "첫 줄"})
    assert app.background.get("1.0", "end").strip() == "첫 줄"
    app._on_info({"title": "t", "duration": 1, "description": "다른"})   # box not empty, owner says no
    assert app.background.get("1.0", "end").strip() == "첫 줄"
    assert "（2:05）" in app.log.get("1.0", "end")

