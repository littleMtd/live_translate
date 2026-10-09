"""Offline contracts: providers mocked, local ffmpeg integration optional."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from modules import offline_subtitles as off

FIXTURES = Path(__file__).parent / "fixtures" / "subtitles"
ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from modules.profile_context import profile_state
    monkeypatch.setattr(profile_state, "_snapshot", profile_state.current())
    monkeypatch.setattr(profile_state, "_generation", profile_state._generation)
    import urllib.request
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

def test_appendix_b():
    cues = off.parse_subtitles((FIXTURES / "youtube_auto_ko.vtt").read_text(encoding="utf8"))
    expected = [
        (4, 8.505, "우우우"), (8.515, 11.509, "불 꺼진"),
        (11.519, 14.910, "않는 말도 없는"),
        (14.920, 18.990, "화 하나님이 속가 보이지 않아 괜찮은"),
        (19, 21.605, "적게도 티가"), (22.439, 24.950, "한 발짝"),
        (24.960, 26.710, "조심술에"), (26.720, 28.750, "다가갈게"),
        (28.760, 30.310, "오늘이")]
    assert [(c.start, c.end, c.text) for c in cues] == expected

def test_official_fixture():
    raw = (FIXTURES / "youtube_official_ko.vtt").read_text(encoding="utf8")
    cues = off.parse_subtitles(raw)
    # Independent extraction from the human supplied official source.
    expected = []
    for block in raw.split("\n\n"):
        lines = block.splitlines()
        idx = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if idx is not None:
            value = " ".join(" ".join(lines[idx + 1:]).split())
            if value:
                expected.append(value)
    assert [c.text for c in cues] == expected
    assert cues

@pytest.mark.parametrize("srt", [False, True])
def test_roundtrip(srt):
    cues = [off.Cue("c1", 0.123, 2.999, "안녕 & <hello>")]
    assert off.parse_subtitles(off.render(cues, srt), automatic=False) == cues

def test_rolling_conversation_and_real_repetition():
    raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n안녕\n\n"
           "00:00:01.000 --> 00:00:02.000\n안녕\n\n"
           "00:00:02.000 --> 00:00:03.000\n안녕\n친구\n")
    assert [c.text for c in off.parse_subtitles(raw, automatic=True)] == ["안녕", "안녕", "친구"]
    assert [c.text for c in off.parse_subtitles(raw, automatic=False)] == ["안녕", "안녕", "안녕 친구"]

def test_invalid_and_incomplete_cues():
    raw = ("1\n00:00:02,000 --> 00:00:01,000\nwrong\n\n"
           "2\n00:00:03,000 --> 00:00:04,000\n\n"
           "3\n00:00:05,000 --> broken\nwrong\n\n"
           "4\n00:00:06,000 --> 00:00:07,000\nok")
    assert [c.text for c in off.parse_subtitles(raw)] == ["ok"]

def test_slice_ownership_and_invalid_segments():
    parts = off.slices(1201)
    assert parts == [off.Slice(0, 605, 0, 600), off.Slice(595, 1201, 600, 1200),
                     off.Slice(1195, 1201, 1200, float("inf"))]  # last slice owns past the end
    first = [{"start": 599, "end": 601, "text": "반복"}]
    assert not off.segment_cues(first, parts[0], 1201)
    second = [{"start": 4, "end": 6, "text": "반복"},
              {"start": 6, "end": 7, "text": "반복"},
              {"start": float("nan"), "end": 8, "text": "bad"},
              {"start": -1, "end": 2, "text": "bad"},
              {"start": 3, "end": 3, "text": "bad"}]
    cues = off.segment_cues(second, parts[1], 1201)
    assert [(c.start, c.end, c.text) for c in cues] == [
        (599, 601, "반복"), (601, 602, "반복")]
    assert off.segment_cues([{"start": 4, "end": 20, "text": "끝"}],
                           off.Slice(1195, 1201, 1195, 1220), 1201)[0].end == 1201

@pytest.mark.parametrize("duration,length,overlap", [(0,600,5),(10,0,5),(10,10,10),
                                                    (float("nan"),600,5)])
def test_invalid_slices(duration, length, overlap):
    with pytest.raises(ValueError):
        off.slices(duration, length, overlap)

def fake_engine(outputs=None):
    from modules.translation_engines import DeepSeekTranslationEngine
    engine = DeepSeekTranslationEngine()
    calls = []
    def send(messages, *, route_request):
        calls.append(route_request)
        rows = json.loads(messages[-1][1])["cues"]
        value = (outputs.pop(0) if outputs else
                 [{"id": r["id"], "zh": "你好"} for r in rows])
        return SimpleNamespace(text=value if isinstance(value, str) else json.dumps(value),
                               finish_reason="stop")
    engine.translate_messages = send
    engine.calls = calls
    return engine

@pytest.mark.parametrize("bad", ["not json", [], [{"id":"c1","zh":"a"},{"id":"c1","zh":"b"}],
    [{"id":"x","zh":"a"},{"id":"c2","zh":"b"}],
    [{"zh":"a"},{"id":"c2","zh":"b"}],
    [{"id":"c1","zh":1},{"id":"c2","zh":"b"}]])
def test_batch_invalid_falls_back(bad):
    cues = [off.Cue("c1",0,1,"안녕"), off.Cue("c2",1,2,"친구")]
    engine = fake_engine([bad])
    targets, report = off.translate(cues, off.profile_scope("isegye_lilpa"), engine, Mock())
    assert [c.text for c in targets] == ["你好", "你好"]
    assert len(engine.calls) == 3
    assert not report["failed_cues"]

def test_request_route_and_reordered_ids():
    engine = fake_engine([[{"id":"c2","zh":"二"},{"id":"c1","zh":"一"}]])
    rows = [{"id":"c1","ko":"안녕"},{"id":"c2","ko":"친구"}]
    assert off.request_batch(engine, rows, "FACT", []) == {"c2":"二", "c1":"一"}
    route = engine.calls[0]
    assert route.timeout_seconds == 60
    assert 512 <= json.loads(route.body)["max_tokens"] <= 8192
    assert "FACT" in route.messages[0][1]
    assert "Preserve every cue ID" in route.messages[0][1]

def test_truncated_response():
    engine = fake_engine()
    engine.translate_messages = lambda *a, **k: SimpleNamespace(text="[]",finish_reason="length")
    with pytest.raises(ValueError):
        off.request_batch(engine, [{"id":"c1","ko":"안녕"}], "", [])

def test_failure_marker_and_quality_flags():
    engine = fake_engine(["bad", "bad", [{"id":"c2","zh":"未核准 한국어"}]])
    targets, report = off.translate(
        [off.Cue("c1",0,1,"안녕"),off.Cue("c2",1,2,"친구")],
        off.profile_scope("isegye_lilpa"), engine, Mock())
    assert targets[0].text == "【未翻譯】안녕"
    assert report["failed_cues"] == ["c1"]
    assert report["quality_flags"] == [{"id":"c2","flag":"unapproved_korean"}]

def test_profile_corrections_isolation_and_context(monkeypatch):
    from config import cfg
    from modules.activity_context import effective_activity_value, effective_profile_id
    from modules.profile_context import profile_state
    from modules import translator
    before = (cfg.translation, cfg.stt)
    previous = profile_state.current()
    engine = fake_engine([[{"id":"c1","zh":"高世久 세구땅 简体"}],
                          [{"id":"c2","zh":"릴파"}]])
    original = engine.translate_messages
    def send(messages, **kwargs):
        assert effective_profile_id() == "isegye_lilpa"
        assert effective_activity_value() == ""
        return original(messages, **kwargs)
    engine.translate_messages = send
    try:
        snapshot = off.profile_scope("isegye_lilpa")
        assert snapshot.mode == "manual"
        targets, report = off.translate(
            [off.Cue("c1",0,1,"고세구 세구땅"),off.Cue("c2",1,2,"늘파")],
            snapshot, engine, Mock(), batch_size=1)
        assert "Gosegu" in targets[0].text
        assert "세구땅" in targets[0].text
        assert "簡體" in targets[0].text
        assert json.loads(engine.calls[1].messages[-1][1])["cues"][0]["ko"] == "릴파"
        assert len(json.loads(engine.calls[1].messages[-1][1])["context"]) == 1
        assert cfg.translation is before[0] and cfg.stt is before[1]
        from utils.runtime_events import runtime_events
        assert report["run_kind"] == runtime_events.run_kind
    finally:
        profile_state.configure_source(previous.source_profile_id, mode=previous.mode)

def test_deliver_and_rename_rollback(tmp_path, monkeypatch):
    cues = [off.Cue("c1",0,1,"안녕")]
    rename = os.rename
    calls = 0
    def fail_second(src,dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("rename failure")
        rename(src,dst)
    monkeypatch.setattr(os,"rename",fail_second)
    with pytest.raises(OSError):
        off.deliver(tmp_path,"a",cues,cues,{})
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(os,"rename",rename)
    files = off.deliver(tmp_path,"a",cues,cues,{})
    assert len(files) == 4
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(FileExistsError):
        off.deliver(tmp_path,"a",cues,cues,{})
    assert before == {p.name:p.read_bytes() for p in tmp_path.iterdir()}

def test_estimate_unknown_and_overlap():
    result = off.estimate([],1201,model="unknown",translation_model="unknown")
    assert result["stt_seconds"] == 605 + 606 + 10
    assert result["stt_cost_usd"] is None
    assert result["translation_cost_usd"] is None
    assert off.estimate([],1)["stt_seconds"] == 10

def mock_audio(monkeypatch, size=100):
    def run(args,**kwargs):
        Path(args[-1]).write_bytes(b"x"*size)
    monkeypatch.setattr(off.subprocess,"run",run)

def test_transcribe_retry_same_slice_and_limits(tmp_path,monkeypatch):
    mock_audio(monkeypatch)
    error = RuntimeError("rate")
    error.status_code = 429
    error.response = SimpleNamespace(headers={"Retry-After":"2"})
    create = Mock(side_effect=[error,{"segments":[{"start":0,"end":1,"text":"안녕"}]}])
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    sleep = Mock()
    progress = Mock()
    cues = off.transcribe(tmp_path/"a.wav",1,"ffmpeg",client,"prompt","model",progress,sleep=sleep)
    assert cues == [off.Cue("c1",0,1,"안녕")]
    assert create.call_count == 2
    sleep.assert_called_once_with(2)
    create.side_effect = error
    with pytest.raises(RuntimeError):
        off.transcribe(tmp_path/"a.wav",1,"ffmpeg",client,"prompt","model",progress,
                       sleep=sleep,max_wait=1)
    create.assert_called()
    mock_audio(monkeypatch,off.UPLOAD_LIMIT)
    create.reset_mock()
    with pytest.raises(ValueError):
        off.transcribe(tmp_path/"a.wav",1,"ffmpeg",client,"prompt","model",progress)
    create.assert_not_called()

def test_local_ffmpeg_mock_groq(tmp_path):
    try:
        encoder,probe = off.media_tools()
    except ValueError:
        pytest.skip("ffmpeg/ffprobe unavailable")
    wav=tmp_path/"tone.wav"
    subprocess.run([encoder,"-nostdin","-v","error","-f","lavfi","-i",
                    "sine=frequency=440:duration=1","-y",str(wav)],capture_output=True,check=True)
    duration=off.media_duration(wav,probe)
    create=Mock(return_value={"segments":[{"start":0.1,"end":0.9,"text":"안녕"}]})
    client=SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    assert off.transcribe(wav,duration,encoder,client,"prompt","model",Mock())[0].start == .1

@pytest.mark.parametrize("args,code,stage", [
    (["translate","--input","tests/fixtures/subtitles/youtube_auto_ko.vtt","--estimate"],0,"estimate"),
    (["translate","--input","missing.vtt","--estimate"],1,"error"),
    (["invalid"],1,"error")])
def test_real_cli_json_only(args,code,stage):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", LIVE_TRANSLATE_RUN_KIND="natural",
               GROQ_API_KEY="", DEEPSEEK_API_KEY="")
    run = subprocess.run([sys.executable,str(ROOT/"scripts"/"make_subtitles.py"),*args],
                         cwd=ROOT,capture_output=True,text=True,encoding="utf8",env=env)
    assert run.returncode == code, run.stderr
    values = [json.loads(line) for line in run.stdout.splitlines()]
    assert values[-1]["stage"] == stage
    assert all(value["job"] for value in values)
    if stage == "estimate":
        assert values[-1]["cue_count"] == 9

def test_estimate_never_constructs_providers(monkeypatch,capsys):
    spec=importlib.util.spec_from_file_location("make_subtitles",ROOT/"scripts"/"make_subtitles.py")
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setenv("LIVE_TRANSLATE_RUN_KIND", "natural")
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "configure_logging", lambda: None)
    from modules import translation_engines
    monkeypatch.setattr(translation_engines,"DeepSeekTranslationEngine",
                        Mock(side_effect=AssertionError("provider constructed")))
    assert module.main(["translate","--input",str(FIXTURES/"youtube_auto_ko.vtt"),
                        "--estimate"]) == 0
    assert os.environ["LIVE_TRANSLATE_RUN_KIND"] == "cafe_clip"
    assert json.loads(capsys.readouterr().out)["stage"] == "estimate"

def test_adapter_wire_body_mock(monkeypatch):
    import urllib.request
    from modules.translation_engines import DeepSeekTranslationEngine
    engine = DeepSeekTranslationEngine()
    engine._api_key = "mock-key"
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"choices":[{"finish_reason":"stop",
        "message":{"content":'[{"id":"c1","zh":"你好"}]'}}]}).encode()
    sender = Mock(return_value=response)
    monkeypatch.setattr(urllib.request,"urlopen",sender)
    assert off.request_batch(engine,[{"id":"c1","ko":"안녕"}],"PROFILE FACT",[]) == {"c1":"你好"}
    request = sender.call_args.args[0]
    body = json.loads(request.data)
    assert "PROFILE FACT" in body["messages"][0]["content"]
    assert body["max_tokens"] == 512
    assert sender.call_args.kwargs["timeout"] == 60

def test_cli_mock_job_done_in_subprocess(tmp_path):
    # Exercise real CLI/profile/logging/output in a fresh interpreter, with all
    # provider sends replaced. This catches stray repo logging on stdout.
    harness = """
import runpy, sys, json
from types import SimpleNamespace
import urllib.request
def forbidden(*a, **k): raise AssertionError('network forbidden')
urllib.request.urlopen = forbidden
from modules.translation_engines import DeepSeekTranslationEngine
def send(self, messages, *, route_request):
    rows=json.loads(messages[-1][1])['cues']
    return SimpleNamespace(text=json.dumps([{'id':r['id'],'zh':'你好'} for r in rows]),finish_reason='stop')
DeepSeekTranslationEngine.translate_messages=send
DeepSeekTranslationEngine.available=property(lambda self: True)
sys.argv=['scripts/make_subtitles.py','translate','--input',sys.argv[1],
          '--out-dir',sys.argv[2],'--profile','isegye_lilpa']
runpy.run_path('scripts/make_subtitles.py',run_name='__main__')
"""
    run = subprocess.run([sys.executable,"-X","utf8","-c",harness,
                          str(FIXTURES/"youtube_auto_ko.vtt"),str(tmp_path)],
                         cwd=ROOT,capture_output=True,text=True,encoding="utf8")
    assert run.returncode == 0, run.stderr
    values=[json.loads(line) for line in run.stdout.splitlines()]
    assert [v["stage"] for v in values] == ["translate","done"]
    assert values[-1]["report"]["run_kind"] == "cafe_clip"
    assert len(values[-1]["files"]) == 4
    assert "Profile current" in run.stderr

def test_ffmpeg_missing_is_clear(monkeypatch):
    monkeypatch.setattr(off.shutil,"which",lambda x: None)
    with pytest.raises(ValueError,match="ffmpeg/ffprobe"):
        off.media_tools()

def test_correction_record_reset_per_cue(monkeypatch):
    from modules import translator
    seen=[]
    original=translator._normalize_source_before_matching
    def normalize(source):
        assert translator.get_corrections() == []
        seen.append(source)
        return original(source)
    monkeypatch.setattr(translator,"_normalize_source_before_matching",normalize)
    off.translate([off.Cue("c1",0,1,"늘파"),off.Cue("c2",1,2,"안녕")],
                  off.profile_scope("isegye_lilpa"),fake_engine(),Mock())
    assert len(seen) == 2


# --- Post-implementation review fixes (2026-10-09, Claude Code) ---

def test_cli_uses_offline_stt_timeout_not_live_default():
    from config import cfg
    assert off.OFFLINE_STT_TIMEOUT_SECONDS >= 120
    assert off.OFFLINE_STT_TIMEOUT_SECONDS > cfg.stt.groq_timeout
    source = (Path(__file__).resolve().parents[1] / "scripts" / "make_subtitles.py").read_text(encoding="utf-8")
    assert "timeout=offline.OFFLINE_STT_TIMEOUT_SECONDS" in source
    assert "cfg.stt.groq_timeout" not in source

def test_last_slice_keeps_segment_overshooting_media_end():
    parts = off.slices(31.2)
    cues = off.segment_cues([{"start": 29, "end": 34, "text": "마지막"}], parts[-1], 31.2)
    assert [(c.start, c.end, c.text) for c in cues] == [(29, 31.2, "마지막")]

def test_fenced_json_reply_is_accepted():
    engine = fake_engine(['```json\n[{"id": "c1", "zh": "你好"}]\n```'])
    assert off.request_batch(engine, [{"id": "c1", "ko": "안녕"}], "", []) == {"c1": "你好"}

def test_simplified_script_flag_fires_on_converted_text():
    engine = fake_engine([[{"id": "c1", "zh": "这个视频"}]])
    targets, report = off.translate([off.Cue("c1", 0, 1, "이 영상")],
                                    off.profile_scope("isegye_lilpa"), engine, Mock())
    assert "这" not in targets[0].text
    assert {"id": "c1", "flag": "simplified_script"} in report["quality_flags"]

def test_every_cue_failing_is_an_error():
    engine = fake_engine()
    engine.translate_messages = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DeepSeek HTTP 401"))
    with pytest.raises(RuntimeError, match="every cue"):
        off.translate([off.Cue("c1", 0, 1, "안녕"), off.Cue("c2", 1, 2, "친구")],
                      off.profile_scope("isegye_lilpa"), engine, Mock())

def test_report_run_kind_comes_from_runtime_writer(monkeypatch):
    from utils import runtime_events as module
    monkeypatch.setattr(module.runtime_events, "run_kind", "cafe_clip")
    _targets, report = off.translate([off.Cue("c1", 0, 1, "안녕")],
                                     off.profile_scope("isegye_lilpa"), fake_engine(), Mock())
    assert report["run_kind"] == "cafe_clip"
    monkeypatch.setattr(module.runtime_events, "run_kind", "test")
    _targets, report = off.translate([off.Cue("c1", 0, 1, "안녕")],
                                     off.profile_scope("isegye_lilpa"), fake_engine(), Mock())
    assert report["run_kind"] == "test"


# --- 2026-10-09: estimate by duration and user-visible local errors (Claude Code) ---

def _cli(*args, extra_env=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", GROQ_API_KEY="", DEEPSEEK_API_KEY="",
               PATH="", **(extra_env or {}))  # empty PATH: ffmpeg must not be needed for --duration
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_subtitles.py"), *args],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf8", env=env)
    return run, [json.loads(line) for line in run.stdout.splitlines()]

def test_transcribe_estimate_by_duration_needs_no_input_or_ffmpeg():
    run, values = _cli("transcribe", "--estimate", "--duration", "11244", "--profile", "isegye_lilpa")
    assert run.returncode == 0, run.stderr
    assert values[-1]["stage"] == "estimate"
    assert values[-1]["stt_seconds"] >= 11244
    assert values[-1]["stt_cost_usd"] > 0

@pytest.mark.parametrize("args,message", [
    (("transcribe", "--estimate", "--duration", "0"), "--duration must be a positive number of seconds"),
    (("transcribe", "--estimate", "--duration", "1e12"), "--duration must not exceed 72 hours"),
    (("transcribe", "--duration", "60", "--out-dir", "x"), "--duration is only valid with transcribe --estimate"),
    (("translate", "--estimate", "--duration", "60", "--input", "tests/fixtures/subtitles/youtube_official_ko.vtt"),
     "--duration is only valid with transcribe --estimate"),
    (("transcribe", "--estimate", "--input", "tests/fixtures/subtitles/youtube_official_ko.vtt"),
     "ffmpeg/ffprobe not found; use --ffmpeg with the executable path"),
])
def test_local_validation_errors_are_shown(args, message):
    run, values = _cli(*args)
    assert run.returncode == 1
    assert values[-1] == {**values[-1], "stage": "error", "message": message}

def test_provider_style_errors_stay_generic(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("make_subtitles_generic", ROOT / "scripts" / "make_subtitles.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("LIVE_TRANSLATE_RUN_KIND", "natural")
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "configure_logging", lambda: None)  # keep pytest's captured stdout
    monkeypatch.setattr(off, "parse_subtitles", lambda _text: (_ for _ in ()).throw(RuntimeError("secret sk-123")))
    assert module.main(["translate", "--input", "tests/fixtures/subtitles/youtube_official_ko.vtt", "--estimate"]) == 1
    out = capsys.readouterr().out
    assert "offline job failed" in out and "sk-123" not in out
