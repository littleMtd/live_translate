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
          '--out-dir',sys.argv[2],'--profile','isegye_lilpa','--translator','deepseek']
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


# --- 2026-10-09: Gemini offline translator (plan OFFLINE_GEMINI_PLAN_20261009, R1'/R2'/R3) ---

import email.message
import email.utils
import io
import logging
import urllib.error
from datetime import date, datetime, timedelta, timezone

FAKE_KEY = "AIzaFAKE-SECRET-KEY-0123456789"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def read(self, *a):
        return self.payload


def http_error(code, status="RESOURCE_EXHAUSTED", retry_after=None, body=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    raw = body if body is not None else json.dumps({"error": {"code": code, "status": status,
        "message": f"quota billing details key={FAKE_KEY}"}}).encode()
    return urllib.error.HTTPError(f"https://x/?key={FAKE_KEY}", code, f"err {FAKE_KEY}", headers, io.BytesIO(raw))


def reply(rows, *, finish="STOP", usage=None, text=None, thought=None):
    parts = [{"text": thought, "thought": True}] if thought else []
    parts.append({"text": text if text is not None else json.dumps([{"id": r["id"], "zh": "你好"} for r in rows])})
    data = {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}
    if usage is not False:
        data["usageMetadata"] = usage or {"promptTokenCount": 10, "candidatesTokenCount": 5}
    return data


class FakeSend:
    """Scripted urlopen: each item is an exception, a reply payload, or a callable(rows)."""
    def __init__(self, *script):
        self.script, self.requests = list(script), []
    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        body = json.loads(request.data)
        rows = json.loads(body["contents"][-1]["parts"][0]["text"])["cues"]
        item = self.script.pop(0) if self.script else reply
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item(rows) if callable(item) else item)


def gemini(*script, **kwargs):
    send = FakeSend(*script)
    kwargs.setdefault("sleep", Mock())
    return off.GeminiTranslator(FAKE_KEY, send=send, **kwargs), send


CUES = [off.Cue("c1", 0, 1, "안녕"), off.Cue("c2", 1, 2, "친구")]


def run_translate(engine, cues=CUES, **kwargs):
    return off.translate(cues, off.profile_scope("isegye_lilpa"), engine, Mock(), **kwargs)


def test_gemini_wire_body_and_headers():
    engine, send = gemini()
    assert off.request_batch(engine, [{"id": "c1", "ko": "안녕"}], "PROFILE FACT", []) == {"c1": "你好"}
    request, timeout = send.requests[0]
    body = json.loads(request.data)
    assert request.full_url.endswith("/models/gemini-3.8-flash:generateContent")
    assert FAKE_KEY not in request.full_url
    assert request.get_header("X-goog-api-key") == FAKE_KEY
    assert "PROFILE FACT" in body["systemInstruction"]["parts"][0]["text"]
    config = body["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert config["responseMimeType"] == "application/json"
    assert config["maxOutputTokens"] == 512 + off.GEMINI_THINKING_RESERVE
    assert timeout == 90
    assert FAKE_KEY not in repr(engine)


def test_gemini_402_aborts_job_without_retry_or_per_cue():
    engine, send = gemini(http_error(402))
    with pytest.raises(off.OfflineJobError) as caught:
        run_translate(engine)
    assert str(caught.value) == off.GEMINI_CREDIT_MESSAGE
    assert len(send.requests) == 1
    engine._sleep.assert_not_called()
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("code,status,hint", [
    (400, "INVALID_ARGUMENT", "GEMINI_API_KEY"), (401, "UNAUTHENTICATED", "金鑰無效"),
    (403, "PERMISSION_DENIED", "沒有權限"), (404, "NOT_FOUND", "模型不存在"),
    (403, "<script>" + FAKE_KEY, "沒有權限")])
def test_gemini_client_errors_abort_with_fixed_enum(code, status, hint):
    engine, send = gemini(http_error(code, status))
    with pytest.raises(off.OfflineJobError) as caught:
        run_translate(engine)
    message = str(caught.value)
    assert hint in message and f"HTTP {code}" in message
    assert (status if status in off._GOOGLE_STATUSES else "UNKNOWN") in message
    assert FAKE_KEY not in message and len(send.requests) == 1


def test_gemini_429_is_bounded_retry_not_credit_message():
    engine, send = gemini(*[http_error(429, retry_after="100")] * 10, max_wait=300)
    with pytest.raises(off.OfflineJobError) as caught:
        run_translate(engine)
    assert "速率或配額限制" in str(caught.value) and "餘額" not in str(caught.value)
    assert [c.args[0] for c in engine._sleep.call_args_list] == [100, 100, 100]
    assert len(send.requests) == 4  # never re-sent cue by cue


def test_gemini_retry_budget_is_shared_across_batches():
    engine, send = gemini(http_error(503, "UNAVAILABLE", "100"), reply, http_error(503, "UNAVAILABLE", "100"),
                          max_wait=150)
    with pytest.raises(off.OfflineJobError, match="暫時無法使用"):
        run_translate(engine, batch_size=1)
    assert engine.waited == 100 and len(send.requests) == 3


def test_gemini_network_error_retries_then_succeeds():
    engine, send = gemini(urllib.error.URLError(f"dns {FAKE_KEY}"), TimeoutError(), reply)
    targets, report = run_translate(engine)
    assert [c.text for c in targets] == ["你好", "你好"]
    assert [c.args[0] for c in engine._sleep.call_args_list] == [1, 2]
    assert report["translation_usage"]["requests"] == 1


@pytest.mark.parametrize("value,expected", [("7", 7), ("2.5", 2.5), (None, 2), ("soon", 2), ("-5", 0)])
def test_retry_after_on_urllib_http_error(value, expected):
    assert off._retry_after(http_error(429, retry_after=value), 1) == expected


def test_retry_after_http_date_on_urllib_http_error():
    value = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(hours=1), usegmt=True)
    assert 3500 <= off._retry_after(http_error(429, retry_after=value), 1) <= 3600


def test_max_tokens_batch_then_cue_fallback_and_usage_counted_once():
    usage = {"promptTokenCount": 100, "candidatesTokenCount": 7, "thoughtsTokenCount": 3}
    truncated = lambda rows: reply(rows, finish="MAX_TOKENS", usage=usage, text="[")
    engine, send = gemini(truncated, truncated, reply)
    targets, report = run_translate(engine)
    assert [c.text for c in targets] == ["【未翻譯】안녕", "你好"]
    assert report["failed_cues"] == ["c1"]
    assert report["translation_usage"] == {"requests": 3, "input_tokens": 210, "output_tokens": 19,
                                           "thinking_tokens": 6, "usage_missing_requests": 0}
    assert report["usage_complete"] is True


@pytest.mark.parametrize("bad", [
    lambda rows: reply(rows, finish="SAFETY"), lambda rows: reply(rows, text=""),
    lambda rows: {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 4}},
    lambda rows: reply(rows, text='[{"id":"zz","zh":"x"}]'), b"not json", ["list"]])
def test_rejected_replies_fall_back_and_still_count_usage(bad):
    engine, send = gemini(bad, reply, reply)
    targets, report = run_translate(engine)
    assert [c.text for c in targets] == ["你好", "你好"]
    assert report["translation_usage"]["requests"] == 3


def test_thinking_above_reserve_is_metered_and_thought_parts_ignored():
    # Thinking above the 1024 reserve while answer + thinking (1200) stays under the 1536 request cap.
    usage = {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 1100}
    engine, send = gemini(lambda rows: reply(rows, usage=usage, thought='思考 [{"id":"c1"}]'))
    targets, report = run_translate(engine)
    assert [c.text for c in targets] == ["你好", "你好"]
    assert json.loads(send.requests[0][0].data)["generationConfig"]["maxOutputTokens"] == 1536
    assert report["translation_usage"]["thinking_tokens"] == 1100
    rates = off.gemini_rates(off.GEMINI_MODEL)
    assert report["translation_cost_usd"] == pytest.approx((1000 * rates[0] + 1200 * rates[1]) / 1e6)


def test_only_thought_parts_is_rejected():
    engine, send = gemini(lambda rows: {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "[]", "thought": True}]}}]}, reply, reply)
    _targets, report = run_translate(engine)
    assert report["translation_usage"]["requests"] == 3 and not report["failed_cues"]


def test_missing_usage_metadata_is_reported_incomplete():
    engine, send = gemini(lambda rows: reply(rows, usage=False))
    _targets, report = run_translate(engine)
    assert report["usage_complete"] is False
    assert report["translation_usage"]["usage_missing_requests"] == 1
    assert report["translation_usage"]["requests"] == 1


def test_gemini_rates_switch_on_utc_date():
    assert off.gemini_rates(off.GEMINI_MODEL, date(2026, 12, 31)) == (0.75, 3.75)
    assert off.gemini_rates(off.GEMINI_MODEL, date(2027, 1, 1)) == (1.50, 7.50)
    assert off.gemini_rates("unknown-model", date(2026, 12, 31)) is None
    before = off.estimate([], 600, translator="gemini", today=date(2026, 12, 31))
    after = off.estimate([], 600, translator="gemini", today=date(2027, 1, 1))
    assert after["translation_cost_usd"][1] == pytest.approx(2 * before["translation_cost_usd"][1])
    assert before["translation_rate_date_utc"] == "2026-12-31"
    assert off.estimate([], 600, translator="gemini", translation_model="x")["translation_cost_usd"] is None


def test_gemini_estimate_cap_is_sum_of_request_limits():
    cues = [off.Cue(f"c{i}", i, i + 1, "가" * 10) for i in range(1, 46)]
    result = off.estimate(cues, translator="gemini")
    # 45 cues -> batches of 20, 20, 5; each request caps answer + thinking together.
    expected = sum(min(off.GEMINI_MAX_OUTPUT, min(8192, max(512, chars * 4 + 80 * n))
                       + off.GEMINI_THINKING_RESERVE) for chars, n in ((200, 20), (200, 20), (50, 5)))
    assert result["translation_output_cap_tokens"] == expected
    assert result["translation_output_cap_tokens"] >= result["translation_output_tokens"][1]


@pytest.fixture
def keys():
    from config import cfg
    original = {name: getattr(cfg.keys, name) for name in ("gemini", "groq", "deepseek")}
    def set_keys(**values):
        for name, value in values.items():
            object.__setattr__(cfg.keys, name, value)
    yield set_keys
    set_keys(**original)


def load_cli(monkeypatch, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "make_subtitles.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("LIVE_TRANSLATE_RUN_KIND", "natural")
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "configure_logging", lambda: None)
    return module


def test_missing_translator_key_fails_before_any_transcription(monkeypatch, capsys, keys, tmp_path):
    keys(gemini="", groq="groq-test")
    module = load_cli(monkeypatch, "make_subtitles_keycheck")
    monkeypatch.setattr(off, "media_tools", lambda ffmpeg=None: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(off, "media_duration", lambda path, probe: 30.0)
    import groq
    monkeypatch.setattr(groq, "Groq", Mock(side_effect=AssertionError("Groq constructed")))
    transcribe = Mock(side_effect=AssertionError("transcribed"))
    monkeypatch.setattr(off, "transcribe", transcribe)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    assert module.main(["transcribe", "--input", str(media), "--out-dir", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out.splitlines()[-1])["message"] == "Gemini API key is missing"
    transcribe.assert_not_called()


def test_estimate_with_gemini_needs_no_key():
    run, values = _cli("transcribe", "--estimate", "--duration", "600", extra_env={"GEMINI_API_KEY": ""})
    assert run.returncode == 0, run.stderr
    assert values[-1]["translator"] == "gemini"
    assert values[-1]["translation_cost_usd"][1] > values[-1]["translation_cost_usd"][0] > 0


@pytest.mark.parametrize("failure", [
    "http500", "http403", "http402", "network", "value", "runtime"])
def test_key_never_leaks_to_stdout_stderr_logs_or_report(monkeypatch, capsys, caplog, keys, tmp_path, failure):
    keys(gemini=FAKE_KEY)
    module = load_cli(monkeypatch, "make_subtitles_leak")
    make = {"http500": lambda: http_error(500, "INTERNAL"), "http403": lambda: http_error(403, "PERMISSION_DENIED"),
            "http402": lambda: http_error(402), "network": lambda: urllib.error.URLError(f"host {FAKE_KEY}"),
            "value": lambda: ValueError(f"bad value {FAKE_KEY}"),
            "runtime": lambda: RuntimeError(f"boom {FAKE_KEY}")}[failure]
    import urllib.request
    calls = []
    def send(request, timeout):
        calls.append(request)
        raise make()
    monkeypatch.setattr(urllib.request, "urlopen", send)
    monkeypatch.setattr(off.time, "sleep", lambda s: None)
    caplog.set_level(logging.DEBUG)
    code = module.main(["translate", "--input", str(FIXTURES / "youtube_official_ko.vtt"),
                        "--out-dir", str(tmp_path), "--profile", "isegye_lilpa"])
    captured = capsys.readouterr()
    reports = [p.read_text(encoding="utf-8") for p in tmp_path.glob("*.report.json")]
    assert code == 1 and calls
    for text in (captured.out, captured.err, caplog.text, *reports):
        assert FAKE_KEY not in text and "FAKE-SECRET" not in text
    if failure == "http402":
        assert off.GEMINI_CREDIT_MESSAGE in captured.out


def test_cli_gemini_job_done_in_subprocess(tmp_path):
    harness = """
import runpy, sys, json, io
import urllib.request
class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def send(request, timeout):
    rows = json.loads(json.loads(request.data)['contents'][-1]['parts'][0]['text'])['cues']
    text = json.dumps([{'id': r['id'], 'zh': '你好'} for r in rows])
    return Response(json.dumps({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}],
        'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 20}}).encode())
urllib.request.urlopen = send
sys.argv=['scripts/make_subtitles.py','translate','--input',sys.argv[1],'--out-dir',sys.argv[2],'--profile','isegye_lilpa']
runpy.run_path('scripts/make_subtitles.py',run_name='__main__')
"""
    env = dict(os.environ, GEMINI_API_KEY=FAKE_KEY)
    run = subprocess.run([sys.executable, "-X", "utf8", "-c", harness, str(FIXTURES / "youtube_auto_ko.vtt"),
                          str(tmp_path)], cwd=ROOT, capture_output=True, text=True, encoding="utf8", env=env)
    assert run.returncode == 0, run.stderr
    values = [json.loads(line) for line in run.stdout.splitlines()]
    assert [v["stage"] for v in values] == ["translate", "done"]
    report = values[-1]["report"]
    assert report["translator"] == "gemini" and report["usage_complete"] is True
    assert report["translation_usage"]["requests"] == 1 and report["translation_cost_usd"] > 0
    assert FAKE_KEY not in run.stdout + run.stderr


def test_live_entry_never_imports_offline_gemini_client():
    code = ("import sys, main; "
            "assert 'modules.offline_subtitles' not in sys.modules, 'offline module imported by live path'")
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert run.returncode == 0, run.stderr


# --- Post-implementation review fixes (Codex round, 2026-10-09) ---

def _chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def test_local_send_error_aborts_once_without_key_or_chain():
    engine, send = gemini(ValueError(f"Invalid header value b'{FAKE_KEY}\n'"))
    with pytest.raises(off.OfflineJobError) as caught:
        run_translate(engine)
    assert len(send.requests) == 1  # not re-sent cue by cue
    assert FAKE_KEY not in str(caught.value) and FAKE_KEY not in repr(caught.value)
    assert _chain(caught.value) == [caught.value]


def test_real_newline_key_never_leaks():
    # Real urllib/http.client stack: header validation rejects the key before any socket opens.
    opener = urllib.request.build_opener()
    engine = off.GeminiTranslator(FAKE_KEY + "\n", send=lambda request, timeout: opener.open(request, timeout=timeout))
    with pytest.raises(off.OfflineJobError) as caught:
        engine.complete((("system", "s"), ("user", json.dumps({"context": [], "cues": []}))), 512)
    assert FAKE_KEY not in str(caught.value) and _chain(caught.value) == [caught.value]


def test_per_cue_abort_does_not_chain_batch_reply():
    secret_reply = "not json " + FAKE_KEY
    engine, send = gemini(lambda rows: reply(rows, text=secret_reply), http_error(402))
    with pytest.raises(off.OfflineJobError) as caught:
        run_translate(engine)
    assert str(caught.value) == off.GEMINI_CREDIT_MESSAGE
    assert _chain(caught.value) == [caught.value]
    assert len(send.requests) == 2


def test_gemini_estimate_cap_uses_actual_uneven_batches():
    cues = [off.Cue(f"c{i}", i, i + 1, "가") for i in range(1, 21)] + [off.Cue("c21", 21, 22, "가" * 2000)]
    result = off.estimate(cues, translator="gemini")
    assert result["translation_output_cap_tokens"] == (20 * 4 + 80 * 20 + 1024) + (2000 * 4 + 80 + 1024) == 11808


def test_gemini_model_flag(monkeypatch, capsys, keys, tmp_path):
    keys(gemini=FAKE_KEY)
    module = load_cli(monkeypatch, "make_subtitles_model")
    seen = []
    def send(request, timeout):
        seen.append(request.full_url)
        rows = json.loads(json.loads(request.data)["contents"][-1]["parts"][0]["text"])["cues"]
        return FakeResponse(reply(rows))
    monkeypatch.setattr(urllib.request, "urlopen", send)
    assert module.main(["translate", "--input", str(FIXTURES / "youtube_auto_ko.vtt"), "--out-dir", str(tmp_path),
                        "--gemini-model", "gemini-9-test"]) == 0
    assert seen and all("/models/gemini-9-test:" in url for url in seen)
    done = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert done["report"]["translation_model"] == "gemini-9-test"
    assert done["report"]["translation_cost_usd"] is None  # unknown model price
    assert done["report"]["prompt_version"] == "offline-subtitles-v2"
    assert module.main(["translate", "--input", str(FIXTURES / "youtube_auto_ko.vtt"), "--out-dir", str(tmp_path),
                        "--translator", "deepseek", "--gemini-model", "x"]) == 1
    assert module.main(["translate", "--input", str(FIXTURES / "youtube_auto_ko.vtt"), "--out-dir", str(tmp_path),
                        "--gemini-model", "../evil?key=1"]) == 1


# --- Post-implementation re-review fixes (Codex round 2, 2026-10-09) ---

@pytest.mark.parametrize("profile,text", [("isegye_lilpa", "소울라인"), ("isegye_lilpa", "늘파"), ("", "안녕")])
def test_estimate_cap_matches_real_request_caps_after_normalization(profile, text):
    cues = [off.Cue(f"c{i}", i, i + 1, text) for i in range(1, 46)]
    snapshot = off.profile_scope(profile)
    engine, send = gemini()
    off.translate(cues, snapshot, engine, Mock())
    actual = sum(json.loads(r.data)["generationConfig"]["maxOutputTokens"] for r, _t in send.requests)
    result = off.estimate(cues, translator="gemini", sources=off.normalized_sources(cues, snapshot))
    assert result["translation_output_cap_tokens"] == actual


@pytest.mark.parametrize("extra", [["--gemini-model", "../evil?key=1"], ["--translator", "deepseek", "--gemini-model", "x"]])
def test_gemini_model_flag_is_validated_before_estimate(extra):
    run, values = _cli("translate", "--estimate", "--input", "tests/fixtures/subtitles/youtube_official_ko.vtt", *extra)
    assert run.returncode == 1
    assert values[-1]["message"] == "--gemini-model must be a Gemini model name and needs --translator gemini"
