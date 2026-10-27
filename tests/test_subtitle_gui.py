"""scripts/subtitle_gui.py helpers (no window, no child process)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import subtitle_gui  # noqa: E402


def test_command_carries_every_option():
    command = subtitle_gui.build_command("https://youtu.be/x", lyrics=True, profile="stellive_hina",
                                         out="D:/subs", prefer_audio=True, assume_yes=True)
    assert command[1:4] == [str(subtitle_gui.TOOL), "https://youtu.be/x", "--no-open"]
    assert command[command.index("--out") + 1] == "D:/subs"
    assert command[command.index("--profile") + 1] == "stellive_hina"
    assert {"--lyrics", "--prefer-audio", "--yes"} <= set(command)
    plain = subtitle_gui.build_command("u", lyrics=False, profile="", out="o", prefer_audio=False,
                                       assume_yes=False)
    assert not {"--lyrics", "--prefer-audio", "--yes", "--profile", "--background"} & set(plain)
    noted = subtitle_gui.build_command("u", lyrics=True, profile="", out="o", prefer_audio=False,
                                       assume_yes=False, background="  兩人合唱\n藍色是姊姊  ")
    assert noted[noted.index("--background") + 1] == "兩人合唱\n藍色是姊姊"
    blank = subtitle_gui.build_command("u", lyrics=True, profile="", out="o", prefer_audio=False,
                                       assume_yes=False, background=" \n ")
    assert "--background" not in blank


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
