from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image
import pytest

from config import cfg
from modules.profile_context import ProfileState, profile_state
import modules.scene_context as scene_context
from modules.scene_context import (
    CaptureFrame,
    PrintWindowCaptureBackend,
    SafeWindowResolver,
    SceneContextUpdater,
    WindowIdentity,
)
from modules.scene_vision import (
    VisionAttemptDiagnostics,
    VisionClassification,
    VisionDiagnostics,
    VisionProviderFailure,
)
import modules.scene_vision as scene_vision
from modules.identity_roi import NormalizedRoi

# Any prompt works for the provider-adapter tests below.
_VISION_PROMPT = 'Return exactly one minified JSON object: {"identity":"<name>"}.'


@pytest.fixture
def real_groq():
    previous = sys.modules.pop("groq", None)
    try:
        module = importlib.import_module("groq")
        yield module
    finally:
        sys.modules.pop("groq", None)
        if previous is not None:
            sys.modules["groq"] = previous


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class WindowSource:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.enumerate_count = 0
        self.on_enumerate = None

    def enumerate(self):
        self.enumerate_count += 1
        if self.on_enumerate is not None:
            self.on_enumerate(self.enumerate_count)
        return list(self.candidates)

    def inspect(self, hwnd):
        return next(
            (item for item in self.candidates if item.hwnd == hwnd),
            None,
        )


class CaptureSequence:
    name = "fake_window_only"

    def __init__(self, frames, after_capture=None):
        self.frames = list(frames)
        self.calls = []
        self.after_capture = after_capture

    def capture(self, identity):
        self.calls.append(identity)
        if len(self.frames) > 1:
            result = self.frames.pop(0)
        else:
            result = self.frames[0]
        if self.after_capture is not None:
            self.after_capture()
        return result


class QuerySequence:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def classify(self, jpeg):
        self.calls.append(jpeg)
        if len(self.answers) > 1:
            answer = self.answers.pop(0)
        else:
            answer = self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer()
        if (
            isinstance(answer, str)
            and "\n" not in answer
            and "\r" not in answer
            and not answer.lstrip().startswith("{")
        ):
            if answer.casefold() == "unknown":
                return json.dumps(
                    {"kind": "unknown", "label": ""},
                    separators=(",", ":"),
                )
            return json.dumps(
                {"kind": "game", "label": answer},
                separators=(",", ":"),
                ensure_ascii=False,
            )
        return answer


def window(
    *,
    hwnd=100,
    pid=10,
    title="SOOP stream - Google Chrome",
    platform="soop",
):
    return WindowIdentity(
        hwnd=hwnd,
        pid=pid,
        class_name="Chrome_WidgetWin_1",
        platform=platform,
        title=title,
        bbox=(0, 0, 1000, 700),
    )


def frame(value: int, *, delta_index: int | None = None, content_crop=True):
    thumb = bytearray([value] * 4096)
    if delta_index is not None:
        thumb[delta_index] = min(255, value + 1)
    thumb_bytes = bytes(thumb)
    return CaptureFrame(
        status="ok",
        thumb=thumb_bytes,
        jpeg=b"jpeg-" + bytes([value]),
        fingerprint=hashlib.blake2s(thumb_bytes, digest_size=16).digest(),
        frame_quality="ok",
        content_crop=content_crop,
    )


def image_frame(value: int = 40):
    image = Image.new("RGB", (320, 180), (value, value + 10, value + 20))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    base = frame(value)
    return replace(base, jpeg=buffer.getvalue())


def image_frame_with_identity_block(value: int):
    image = Image.new("RGB", (320, 180), (40, 50, 60))
    for x in range(0, 140):
        for y in range(100, 180):
            image.putpixel((x, y), (value, value, value))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    base = frame(value)
    return replace(base, jpeg=buffer.getvalue())


def make_updater(
    *,
    candidates=None,
    frames=None,
    answers=None,
    clock=None,
    manual=None,
    events=None,
    **kwargs,
):
    clock = clock or Clock()
    source = WindowSource(candidates or [window()])
    resolver = SafeWindowResolver(
        enumerate_windows=source.enumerate,
        inspect_window=source.inspect,
    )
    capture = CaptureSequence(
        frames or [frame(40)],
        after_capture=kwargs.pop("after_capture", None),
    )
    # Automatic activity detection was removed on 2026-11-10. This provider is
    # never attached to the updater; tests keep it to assert that no
    # activity-style vision call happens.
    provider = QuerySequence(answers or ["StarCraft"])
    manual_box = manual if manual is not None else {"value": ""}
    emitted = events if events is not None else []

    def sink(event_type, **fields):
        emitted.append({"event_type": event_type, **fields})

    updater = SceneContextUpdater(
        resolver=resolver,
        capture_backend=capture,
        clock=clock,
        utc_now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        event_sink=sink,
        manual_activity_getter=lambda: manual_box["value"],
        profile_resolution_enabled=kwargs.pop("profile_resolution_enabled", False),
        identity_roi_store=kwargs.pop(
            "identity_roi_store",
            SimpleNamespace(get=lambda _key: None),
        ),
        **kwargs,
    )
    return updater, source, capture, provider, manual_box, emitted, clock


class FixedRoiStore:
    def __init__(self, roi):
        self.roi = roi

    def get(self, _key):
        return self.roi


def test_calibrated_identity_roi_is_authoritative_and_bypasses_whole_scene_markers():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    whole_scene = QuerySequence([
        '{"profile_id":"isegye_lilpa","matched_markers":["isegye_member_viichan"]}'
    ])
    identity_reader = QuerySequence(['{"identity":"솜망"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame()],
            profile_resolution_enabled=True,
            profile_vision_provider=whole_scene,
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
    assert state.current().effective_profile_id == "url"
    assert state.current().evidence_source == "authoritative_identity_roi"
    assert len(identity_reader.calls) == 1
    assert whole_scene.calls == []
    event = next(item for item in events if item["event_type"] == "profile_resolution")
    assert event["identity_roi_calibration_key"] == "soop"
    assert event["normalized_observed_identity"] == "솜망"
    assert event["reviewed_member_match"] == "url_member_sommyang"
    assert event["activation_decision"] == "authoritative_identity_confirmed"


def test_calibrated_roi_uses_reviewed_sompunch_ocr_alias_from_runtime_evidence():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"숨주먹"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame()],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
    assert state.current().effective_profile_id == "hades_chxxnnx"
    event = next(item for item in events if item["event_type"] == "profile_resolution")
    assert event["reviewed_member_match"] == "hades_member_sompunch"
    assert event["activation_decision"] == "authoritative_identity_confirmed"


@pytest.mark.parametrize("observed", ["릴파♪", "릴파♬"])
def test_calibrated_roi_confirms_reviewed_lilpa_music_variants(observed):
    state = ProfileState(profile_state.registry, source_profile_id="mwmeu")
    identity_reader = QuerySequence([json.dumps({"identity": observed}, ensure_ascii=False)])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame()],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
    assert state.current().effective_profile_id == "isegye_lilpa"
    event = next(item for item in events if item["event_type"] == "profile_resolution")
    assert event["reviewed_member_match"] == "isegye_member_lilpa"
    assert event["activation_decision"] == "authoritative_identity_confirmed"


def test_unknown_identity_roi_retains_confirmed_profile_and_does_not_fallback():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    state.confirm_content("url", evidence_source="authoritative_identity_roi")
    identity_reader = QuerySequence(['{"identity":"unreviewed channel"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame()],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
    assert state.current().effective_profile_id == "url"
    assert state.current().confirmation_state == "confirmed"
    event = [
        item for item in events if item["event_type"] == "profile_resolution"
    ][-1]
    assert event["status"] == "unknown"
    assert event["activation_decision"] == "retain_confirmed_profile"


def test_stable_unsupported_identity_roi_keeps_the_profile():
    # Owner decision 2026-10-16: an unrecognized name block ("orts" while a 마냥
    # stream played, run 20261013) never drops the profile to general.
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    state.confirm_content("url", evidence_source="authoritative_identity_roi")
    identity_reader = QuerySequence([
        '{"identity":"unsupported channel"}',
        '{"identity":"unsupported channel"}',
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, clock = make_updater(
            frames=[image_frame(40), image_frame(100)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
        clock.advance(15)
        updater.tick()

    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "url"
    assert state.current().confirmation_state == "confirmed"
    event = [
        item for item in events if item["event_type"] == "profile_resolution"
    ][-1]
    assert event["activation_decision"] == "retain_confirmed_profile"


def test_unchanged_unrecognized_block_is_reread_slowly_and_a_change_reads_at_once():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    state.confirm_content("url", evidence_source="authoritative_identity_roi")
    identity_reader = QuerySequence(['{"identity":"orts"}', '{"identity":"orts"}', '{"identity":"릴파"}'])
    same = image_frame(40)
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, _events, clock = make_updater(
            frames=[same, same, same, same, image_frame(100)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater._profile_fast_gap = 5
        updater.tick()                      # t=0: read "orts"
        for advance in (5, 20):             # t=5, t=25: unchanged, inside 30 s
            clock.advance(advance)
            updater.tick()
        assert len(identity_reader.calls) == 1
        clock.advance(6)                    # t=31: unchanged, gap over
        updater.tick()
        assert len(identity_reader.calls) == 2
        clock.advance(1)                    # t=32: the block changes (stream switch)
        updater.tick()
    assert len(identity_reader.calls) == 3
    assert state.current().effective_profile_id == "isegye_lilpa"


def test_unrecognized_block_rereads_back_off_and_reset_on_change():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    state.confirm_content("url", evidence_source="authoritative_identity_roi")
    identity_reader = QuerySequence(['{"identity":"orts"}'] * 4 + ['{"identity":"other"}'] * 2)
    same = image_frame(40)
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, _events, clock = make_updater(
            frames=[same] * 9 + [image_frame(100)] * 3,
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater._profile_fast_gap = 5
        reads_at = []
        for t in (0, 29, 31, 89, 92, 210, 213, 520, 523, 524, 553, 556):
            clock.advance(t - clock.now) if t > clock.now else None
            before = len(identity_reader.calls)
            updater.tick()
            if len(identity_reader.calls) > before:
                reads_at.append(t)
    # gaps 30, 60, 120, 240 s (cap 300); a changed block reads at once and the gap starts at 30 s again
    assert reads_at == [0, 31, 92, 213, 520, 524, 556]
    assert state.current().effective_profile_id == "url"


def test_calibrated_roi_capture_failure_never_expires_confirmed_profile():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    state.confirm_content("url", evidence_source="authoritative_identity_roi")
    unavailable = CaptureFrame(status="capture_unavailable", frame_quality="unavailable")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, clock = make_updater(
            frames=[unavailable],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=QuerySequence([]),
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
        clock.advance(30)
        updater.tick()
    assert state.current().effective_profile_id == "url"
    profile_events = [item for item in events if item["event_type"] == "profile_resolution"]
    assert profile_events[-1]["activation_decision"] == "retain_confirmed_profile"
    assert profile_events[-1]["stale_profile_cleared"] is False


def _confirmed_roi_updater(state, identity_reader, frames):
    return make_updater(
        frames=frames,
        profile_resolution_enabled=True,
        profile_vision_provider=QuerySequence([]),
        identity_roi_provider=identity_reader,
        identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
    )


def test_confirmed_roi_identity_is_not_reread_until_safety_refresh():
    # Runs 20261002/20261004 re-read an unchanged, confirmed name block every
    # stable gap and half of those reads hit the shared vision rate limit.
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"솜망"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = _confirmed_roi_updater(
            state, identity_reader, [image_frame_with_identity_block(30)]
        )
        updater.tick()
        assert state.current().effective_profile_id == "url"
        assert len(identity_reader.calls) == 1

        for advance in (20, 100, 179):  # t = 20, 120, 299
            clock.advance(advance)
            updater.tick()
        assert len(identity_reader.calls) == 1

        clock.advance(2)  # t = 301, past the 300s safety refresh
        updater.tick()
    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "url"


def test_confirmed_roi_provider_error_defers_retry_instead_of_fast_cadence():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence([
        '{"identity":"솜망"}',
        RuntimeError("rate limited"),
        '{"identity":"솜망"}',
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = _confirmed_roi_updater(
            state, identity_reader, [image_frame_with_identity_block(30)]
        )
        updater.tick()
        clock.advance(301)
        updater.tick()  # safety refresh fails; the confirmed profile is kept
        assert len(identity_reader.calls) == 2
        assert state.current().effective_profile_id == "url"

        clock.advance(20)  # the old fast cadence would retry here
        updater.tick()
        assert len(identity_reader.calls) == 2

        clock.advance(290)
        updater.tick()
    assert len(identity_reader.calls) == 3


@pytest.mark.parametrize(
    "reset",
    (
        lambda state: state.clear_content("profile_registry_changed"),
        lambda state: (
            state.configure_source("isegye_lilpa", mode="manual"),
            state.configure_source("isegye_lilpa", mode="auto"),
        ),
    ),
    ids=("cleared_content", "manual_auto_roundtrip"),
)
def test_profile_state_reset_rereads_confirmed_roi_on_next_tick(reset):
    # The deferral must not keep a cleared/neutral profile for up to 300s when
    # the profile state itself changed under an unchanged name block.
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"솜망"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = _confirmed_roi_updater(
            state, identity_reader, [image_frame_with_identity_block(30)]
        )
        updater.tick()
        assert state.current().effective_profile_id == "url"

        reset(state)
        assert state.current().effective_profile_id != "url"
        clock.advance(20)
        updater.tick()
    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "url"


def test_changed_roi_block_after_confirmation_is_read_immediately():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"솜망"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = _confirmed_roi_updater(
            state,
            identity_reader,
            [
                image_frame_with_identity_block(30),
                image_frame_with_identity_block(30),
                image_frame_with_identity_block(220),
            ],
        )
        updater.tick()
        clock.advance(20)
        updater.tick()
        assert len(identity_reader.calls) == 1
        clock.advance(20)
        updater.tick()  # visibly different name block
    assert len(identity_reader.calls) == 2


def test_changed_identity_roi_cannot_bypass_profile_attempt_budget():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"Ranko"}', '{"identity":"Ranko"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame(40), image_frame(100)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater._profile_max_attempts_per_minute = 1
        updater.tick()
        updater.tick()
    assert len(identity_reader.calls) == 1
    profile_events = [item for item in events if item["event_type"] == "profile_resolution"]
    assert profile_events[-1]["status"] == "throttled"
    assert profile_events[-1]["identity_read_attempted"] is False


def test_identity_roi_provider_failure_records_bounded_vision_diagnostics():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    failure = VisionProviderFailure(
        VisionDiagnostics(
            outcome="error",
            attempt_limit=1,
            error_type="timeout",
            provider="groq",
            model="qwen/qwen3.8-27b",
            retryable=True,
        )
    )
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame(40)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=QuerySequence([failure]),
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()

    event = [item for item in events if item["event_type"] == "profile_resolution"][-1]
    assert event["status"] == "provider_error"
    assert event["vision_outcome"] == "error"
    assert event["vision_error_type"] == "timeout"
    assert event["vision_provider"] == "groq"
    assert event["vision_model"] == "qwen/qwen3.8-27b"
    assert "response" not in repr(event).casefold()


def test_identity_roi_rate_limit_reset_fences_changed_frame_retries():
    state = ProfileState(profile_state.registry, source_profile_id="mwmeu")
    failure = VisionProviderFailure(
        VisionDiagnostics(
            outcome="error",
            attempt_limit=1,
            error_type="rate_limit",
            http_status=429,
            rate_limit_reset_tokens_sec=46.0,
            provider="groq",
            model="qwen/qwen3.8-27b",
            retryable=True,
        )
    )
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, clock = make_updater(
            frames=[
                image_frame_with_identity_block(30),
                image_frame_with_identity_block(220),
                image_frame_with_identity_block(100),
            ],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=QuerySequence([]),
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )
        def delayed_rate_limit():
            clock.advance(3)
            raise failure

        identity_reader = QuerySequence([
            delayed_rate_limit,
            '{"identity":"밍굴_"}',
        ])
        updater._identity_roi_vision = identity_reader
        updater.tick()
        clock.advance(44.5)
        updater.tick()
        assert len(identity_reader.calls) == 1
        clock.advance(3)
        updater.tick()

    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "hades_chxxnnx"
    failure_event = [
        item for item in events
        if item.get("event_type") == "profile_resolution"
        and item.get("status") == "provider_error"
    ][-1]
    assert failure_event["profile_provider_retry_after_sec"] == 47.0


def test_identity_roi_zero_second_rate_reset_uses_fast_gap_floor():
    state = ProfileState(profile_state.registry, source_profile_id="mwmeu")
    failure = VisionProviderFailure(
        VisionDiagnostics(
            outcome="error",
            attempt_limit=1,
            error_type="rate_limit",
            http_status=429,
            rate_limit_reset_tokens_sec=0.0,
        )
    )
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame_with_identity_block(30)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=QuerySequence([failure]),
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )
        updater.tick()

    event = [
        item for item in events
        if item.get("event_type") == "profile_resolution"
    ][-1]
    assert event["profile_provider_retry_after_sec"] == 5.0


def test_identity_read_rescued_by_fallback_route_leaves_no_rate_limit_fence():
    # The fallback route is the same model on a second key with its own quota
    # (2026-10-17), so a read it rescued must not hold the next read for ~45 s.
    state = ProfileState(profile_state.registry, source_profile_id="mwmeu")
    diagnostics = VisionDiagnostics(
        outcome="success",
        attempt_limit=2,
        provider="groq_fallback_key",
        model="qwen/qwen3.8-27b",
        attempt_chain=(
            VisionAttemptDiagnostics(
                provider="groq", model="qwen/qwen3.8-27b", outcome="error",
                retryable=True, error_type="rate_limit", http_status=429,
                rate_limit_reset_tokens_sec=42.0,
            ),
            VisionAttemptDiagnostics(
                provider="groq_fallback_key", model="qwen/qwen3.8-27b",
                outcome="success", retryable=False,
            ),
        ),
    )
    identity_reader = QuerySequence([
        VisionClassification('{"identity":"리"}', diagnostics),
        '{"identity":"릴파"}',
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, _events, clock = make_updater(
            frames=[image_frame_with_identity_block(30), image_frame_with_identity_block(220)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )
        updater.tick()                 # loading page reads "리" via the fallback key
        assert updater._profile_provider_backoff_until == 0.0
        rescued = [e for e in _events if e.get("event_type") == "profile_resolution"][-1]
        assert rescued["vision_provider"] == "groq_fallback_key"
        assert rescued["vision_attempt_count"] == 2
        clock.advance(5)
        updater.tick()                 # the real name shows: read at once
    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "isegye_lilpa"


def test_discarded_identity_roi_read_does_not_publish_new_registry_match():
    state = ProfileState(profile_state.registry, source_profile_id="mwmeu")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, events, _clock = make_updater(
            frames=[image_frame_with_identity_block(30)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=QuerySequence([]),
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )

        def change_generation():
            state.confirm_no_profile()
            return '{"identity":"밍굴_"}'

        updater._identity_roi_vision = QuerySequence([change_generation])
        updater.tick()

    event = [
        item for item in events
        if item.get("event_type") == "profile_resolution"
    ][-1]
    assert event["status"] == "discarded"
    assert event["normalized_observed_identity"] == ""
    assert event["reviewed_member_match"] == ""
    assert event["candidate_profile_id"] == ""


def test_small_roi_capture_noise_does_not_spend_an_identity_read():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"Ranko"}', '{"identity":"Ranko"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, _events, _clock = make_updater(
            # Crosses the old 16-level quantization boundary but is still only
            # a one-level whole-frame brightness fluctuation.
            frames=[image_frame(39), image_frame(40)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 0.5)),
        )
        updater.tick()
        updater.tick()
    assert len(identity_reader.calls) == 1


def test_identity_block_change_forces_read_before_stable_poll_deadline():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence([
        '{"identity":"주르르"}',
        '{"identity":"숨주먹"}',
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, _events, _clock = make_updater(
            frames=[
                image_frame_with_identity_block(30),
                image_frame_with_identity_block(220),
            ],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )
        updater.tick()
        updater.tick()
    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "hades_chxxnnx"


def test_roi_noise_does_not_exhaust_budget_or_hide_a_real_channel_change():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence([
        '{"identity":"주르르"}',
        '{"identity":"숨주먹"}',
    ])
    noisy_frames = [image_frame(value) for value in (39, 40, 41, 40, 39) * 3]
    changed_frame = image_frame_with_identity_block(220)
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _activity, _manual, _events, _clock = make_updater(
            frames=[*noisy_frames, changed_frame],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )
        updater._profile_max_attempts_per_minute = 2
        for _ in noisy_frames:
            updater.tick()
        assert len(identity_reader.calls) == 1
        updater.tick()
    assert len(identity_reader.calls) == 2
    assert state.current().effective_profile_id == "hades_chxxnnx"


def test_destroyed_window_retains_confirmed_profile_until_new_identity_is_confirmed():
    state = ProfileState(profile_state.registry, source_profile_id="url")
    profile_provider = QuerySequence([
        '{"profile_id":"isegye_lilpa","matched_markers":[]}',
        '{"profile_id":"isegye_lilpa","matched_markers":[]}',
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, source, _capture, _provider, _manual, events, clock = make_updater(
            frames=[frame(40), frame(80)],
            answers=["Chatting", "Chatting"],
            profile_vision_provider=profile_provider,
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        updater.tick()
        assert state.current().effective_profile_id == ""
        clock.advance(1)
        updater.tick()
        assert state.current().content_profile_id == "isegye_lilpa"
        assert state.current().effective_profile_id == "isegye_lilpa"
        confirmed_snapshot = state.current()
        assert [event["status"] for event in events if event["event_type"] == "profile_resolution"] == [
            "candidate",
            "confirmed",
        ]

        source.candidates = []
        clock.advance(1)
        updater.tick()
        assert state.current() is confirmed_snapshot
        assert state.current().content_profile_id == "isegye_lilpa"
        assert state.current().effective_profile_id == "isegye_lilpa"
        profile_event = [
            event for event in events
            if event["event_type"] == "profile_resolution"
        ][-1]
        assert profile_event["state_transition"] == "window_to_observation_lost"
        assert profile_event["activation_decision"] == "retain_confirmed_profile"


def test_runtime_url_profile_does_not_fall_back_to_isegye_on_window_loss():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    state.confirm_content("url", evidence_source="authoritative_identity_roi")
    confirmed = state.current()
    with patch.object(scene_context, "profile_state", state):
        updater, source, *_rest = make_updater(
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
        )
        source.candidates = []
        updater.tick()

    assert state.current() is confirmed
    assert state.current().effective_profile_id == "url"
    assert state.current().generation == confirmed.generation
    assert state.current().cache_identity == confirmed.cache_identity


def _profile_result(profile_id, *marker_ids):
    return json.dumps(
        {"profile_id": profile_id, "matched_markers": list(marker_ids)},
        separators=(",", ":"),
    )


def test_cross_profile_member_marker_requires_profile_corroboration():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    answer = _profile_result("url", "url_member_manyang")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=QuerySequence([answer]),
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert state.current().effective_profile_id == ""
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["status"] == "candidate"
    assert event["candidate_streak"] == 1
    assert event["matched_markers"] == ["url_member_manyang"]
    assert event["profile_corroborated"] is False
    assert event["strong_identity_evidence"] is True
    assert event["immediate_activation_eligible"] is False
    assert event["activation_decision"] == "awaiting_cross_profile_consensus"


def test_multi_member_roster_is_not_cross_profile_owner_evidence():
    state = ProfileState(profile_state.registry, source_profile_id="url")
    answer = _profile_result(
        "hades_chxxnnx", "hades_member_chaenna", "hades_member_kyma"
    )
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=QuerySequence([answer, answer]),
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        updater.tick()
        clock.advance(1)
        updater.tick()
    assert state.current().effective_profile_id == ""


def test_cross_profile_member_and_brand_require_two_distinct_frames():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    answer = _profile_result("url", "url_member_moka", "url_brand_group")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=QuerySequence([answer, answer]),
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        updater.tick()
        assert state.current().effective_profile_id == ""
        clock.advance(1)
        updater.tick()
    assert state.current().effective_profile_id == "url"
    profile_events = [event for event in events if event["event_type"] == "profile_resolution"]
    assert [event["status"] for event in profile_events] == ["candidate", "confirmed"]
    assert all(event["profile_corroborated"] for event in profile_events)


def test_unconfirmed_source_profile_does_not_make_member_marker_current():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    answer = _profile_result("isegye_lilpa", "isegye_member_viichan")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=QuerySequence([answer]),
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert state.current().effective_profile_id == ""
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["status"] == "candidate"
    assert event["activation_decision"] == "awaiting_cross_profile_consensus"
    assert event["immediate_activation_eligible"] is False


def test_run_20260904_url_marker_provenance_does_not_activate_without_owner_corroboration():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    unknown = _profile_result("unknown")
    answers = [
        _profile_result("url", "url_member_ranko"),
        unknown,
        _profile_result("url", "url_member_sommyang"),
        unknown,
        _profile_result("url", "url_member_manyang"),
        _profile_result("url", "url_member_manyang"),
        _profile_result("url", "url_member_sommyang"),
        _profile_result("url", "url_member_sommyang"),
        _profile_result(
            "url",
            "url_brand_group",
            "url_member_manyang",
            "url_member_moka",
            "url_member_ranko",
            "url_member_sommyang",
        ),
    ]
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, clock = make_updater(
            frames=[frame(20 + index * 20) for index in range(len(answers))],
            profile_vision_provider=QuerySequence(answers),
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        for _answer in answers:
            updater.tick()
            assert state.current().effective_profile_id == ""
            clock.advance(1)
    assert not any(
        event.get("status") == "confirmed"
        and event.get("candidate_profile_id") == "url"
        for event in events
        if event["event_type"] == "profile_resolution"
    )


def test_medium_brand_marker_keeps_two_frame_consensus():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    answer = _profile_result("url", "url_brand_group")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=QuerySequence([answer, answer]),
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        updater.tick()
        assert state.current().effective_profile_id == ""
        clock.advance(1)
        updater.tick()
    assert state.current().effective_profile_id == "url"
    profile_events = [event for event in events if event["event_type"] == "profile_resolution"]
    assert [event["status"] for event in profile_events] == ["candidate", "confirmed"]
    assert profile_events[0]["marker_strengths"] == ["medium"]


def test_schema_failure_retries_once_but_semantic_rejection_does_not():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    provider = QuerySequence(["not-json", _profile_result("url", "url_member_moka")])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert len(provider.calls) == 2
    assert state.current().effective_profile_id == ""
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["schema_retry_count"] == 1
    assert event["status"] == "candidate"

    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    unsupported = QuerySequence([
        _profile_result("url", "invented_marker"),
        _profile_result("url", "url_member_moka"),
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, *_ = make_updater(
            profile_vision_provider=unsupported,
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert len(unsupported.calls) == 1
    assert state.current().effective_profile_id == ""

    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    duplicate = QuerySequence([
        _profile_result("url", "url_member_moka", "url_member_moka"),
        _profile_result("url", "url_member_moka"),
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, *_ = make_updater(
            profile_vision_provider=duplicate,
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert len(duplicate.calls) == 1
    assert state.current().effective_profile_id == ""


def test_profile_sampling_uses_fast_seeking_and_stable_backoff():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    corroborated = _profile_result("url", "url_member_moka", "url_brand_group")
    provider = QuerySequence([corroborated, corroborated, corroborated])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, _events, clock = make_updater(
            frames=[frame(40), frame(80), frame(120)],
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 5
        updater._profile_stable_gap = 15
        updater.tick()
        clock.advance(5)
        updater.tick()
        assert len(provider.calls) == 2
        clock.advance(10)
        updater.tick()
        assert len(provider.calls) == 2
        clock.advance(5)
        updater.tick()
    assert len(provider.calls) == 3

    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    unknown_provider = QuerySequence([
        _profile_result("unknown"),
        _profile_result("unknown"),
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, _events, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=unknown_provider,
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 5
        updater.tick()
        clock.advance(4)
        updater.tick()
        assert len(unknown_provider.calls) == 1
        clock.advance(1)
        updater.tick()
    assert len(unknown_provider.calls) == 2


def test_sustained_capture_failure_retains_confirmed_profile_and_recovers_fast():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    unavailable = CaptureFrame(status="capture_unavailable", frame_quality="unavailable")
    confirmed = _profile_result("url", "url_member_moka", "url_brand_group")
    provider = QuerySequence([confirmed, confirmed])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, _events, clock = make_updater(
            frames=[frame(40), frame(80), unavailable],
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater._profile_expiry = 300
        updater._profile_fast_gap = 0
        updater.tick()
        clock.advance(1)
        updater.tick()
        assert state.current().effective_profile_id == "url"
        clock.advance(5)
        updater.tick()
        assert state.current().effective_profile_id == "url"
        clock.advance(15)
        updater.tick()
    assert state.current().content_profile_id == "url"
    assert state.current().effective_profile_id == "url"
    assert updater._profile_resolver_state == "capture_failure"
    assert updater._profile_next_call_at == clock.now + updater._profile_fast_gap


def test_profile_event_preserves_provider_diagnostics_and_attempt_budget():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    diagnostics = VisionDiagnostics(
        outcome="success",
        attempt_limit=1,
        provider="groq",
        model="qwen-test",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
    )
    provider = QuerySequence([
        VisionClassification(
            _profile_result("url", "url_member_moka"),
            diagnostics,
        )
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater.tick()
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["vision_provider"] == "groq"
    assert event["vision_total_tokens"] == 120
    assert event["parser_rejection_reason"] == ""
    assert "raw" not in repr(event).lower()


def test_profile_attempt_budget_bounds_schema_retries_and_future_calls():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    provider = QuerySequence(["not-json", "not-json", _profile_result("unknown")])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater._profile_max_attempts_per_minute = 2
        updater._profile_fast_gap = 5
        updater.tick()
        assert len(provider.calls) == 2
        clock.advance(5)
        updater.tick()
    assert len(provider.calls) == 2
    assert any(
        event.get("status") == "throttled"
        and event.get("reason") == "profile_attempt_budget"
        for event in events
    )


def test_profile_attempt_budget_preflights_multi_route_capacity():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    provider = QuerySequence([_profile_result("unknown")])
    provider.route_identities = ("groq:model", "groq:fallback-model")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater._profile_max_attempts_per_minute = 12
        updater._profile_attempt_times.extend([0.0] * 11)
        updater.tick()
    assert provider.calls == []
    assert len(updater._profile_attempt_times) == 11
    assert any(event.get("status") == "throttled" for event in events)


def test_cross_family_member_markers_are_conflict_and_do_not_switch():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    answer = _profile_result("url", "url_member_moka", "hades_member_kyma")
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=QuerySequence([answer]),
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert state.current().effective_profile_id == ""
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["status"] == "rejected"
    assert event["reason"] == "conflicting_identity_markers"


def test_manual_profile_lock_skips_strong_marker_resolution():
    state = ProfileState(
        profile_state.registry,
        source_profile_id="isegye_lilpa",
        mode="manual",
    )
    provider = QuerySequence([_profile_result("url", "url_member_moka")])
    with patch.object(scene_context, "profile_state", state):
        updater, *_ = make_updater(
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert provider.calls == []
    assert state.current().effective_profile_id == "isegye_lilpa"


def test_strong_marker_resolution_ignores_non_safe_crop():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    provider = QuerySequence([_profile_result("url", "url_member_moka")])
    with patch.object(scene_context, "profile_state", state):
        updater, *_ = make_updater(
            frames=[frame(40, content_crop=False)],
            profile_vision_provider=provider,
            profile_resolution_enabled=True,
        )
        updater.tick()
    assert provider.calls == []
    assert state.current().effective_profile_id == ""


def test_strong_marker_result_is_discarded_after_window_identity_changes():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    with patch.object(scene_context, "profile_state", state):
        updater, source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=QuerySequence([]),
            profile_resolution_enabled=True,
        )

        def change_window():
            source.candidates = [window(hwnd=200, pid=20)]
            return _profile_result("url", "url_member_moka")

        updater._profile_vision = QuerySequence([change_window])
        updater.tick()
    assert state.current().effective_profile_id == ""
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["status"] == "discarded"


def test_strong_marker_result_is_discarded_after_registry_reload(tmp_path):
    registry_path = tmp_path / "profiles.json"
    registry_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {"profile_id": "", "label": "General"},
                    {
                        "profile_id": "url",
                        "label": "UR:L",
                        "identity_markers": [
                            {"marker_id": "url_member_moka", "visible_names": ["모카"]}
                        ],
                    },
                    {"profile_id": "isegye_lilpa", "label": "Lilpa"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    from modules.profile_context import load_registry_snapshot

    state = ProfileState(
        load_registry_snapshot(registry_path, version=1),
        source_profile_id="isegye_lilpa",
    )
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
            profile_vision_provider=QuerySequence([]),
            profile_resolution_enabled=True,
        )

        def reload_registry():
            state.reload_registry(registry_path)
            return _profile_result("url", "url_member_moka")

        updater._profile_vision = QuerySequence([reload_registry])
        updater.tick()
    assert state.current().effective_profile_id == ""
    event = next(event for event in events if event["event_type"] == "profile_resolution")
    assert event["status"] == "discarded"
    assert event["reason"] == "profile_generation_changed"


def test_default_window_inspector_accepts_brave_when_platform_title_matches():
    class User32:
        @staticmethod
        def IsWindow(_hwnd):
            return True

        @staticmethod
        def IsWindowVisible(_hwnd):
            return True

        @staticmethod
        def IsIconic(_hwnd):
            return False

    with patch.object(scene_context.sys, "platform", "win32"), \
                patch.object(scene_context, "_user32", return_value=User32()), \
                patch.object(
                    scene_context,
                    "_window_text",
                    return_value="Streamer - SOOP - Brave",
                ), \
                patch.object(
                    scene_context,
                    "_window_class",
                    return_value="Chrome_WidgetWin_1",
                ), \
                patch.object(
                    scene_context,
                    "_window_bbox",
                    return_value=(0, 0, 1200, 800),
                ), \
                patch.object(scene_context, "_window_pid", return_value=99), \
                patch.object(
                    scene_context,
                    "_process_executable_name",
                    return_value="brave.exe",
                ):
        identity = scene_context._inspect_window(123)

    assert identity is not None
    assert identity.platform == "soop"
    assert identity.class_name == "Chrome_WidgetWin_1"


@pytest.mark.parametrize(
    "title",
    (
        "Streamer - SOOP - Microsoft Edge",
        "Brave new world - SOOP - Microsoft Edge",
        "Google Chrome issue - SOOP - Microsoft Edge",
    ),
)
def test_default_window_inspector_rejects_unapproved_chromium_brand(title):
    class User32:
        IsWindow = staticmethod(lambda _hwnd: True)
        IsWindowVisible = staticmethod(lambda _hwnd: True)
        IsIconic = staticmethod(lambda _hwnd: False)

    with patch.object(scene_context.sys, "platform", "win32"), \
            patch.object(scene_context, "_user32", return_value=User32()), \
            patch.object(
                scene_context,
                "_window_text",
                return_value=title,
            ), \
            patch.object(
                scene_context,
                "_window_class",
                return_value="Chrome_WidgetWin_1",
            ), \
            patch.object(
                scene_context,
                "_window_bbox",
                return_value=(0, 0, 1200, 800),
            ), \
            patch.object(scene_context, "_window_pid", return_value=99), \
            patch.object(
                scene_context,
                "_process_executable_name",
                return_value="msedge.exe",
            ):
        assert scene_context._inspect_window(123) is None


def test_window_invalid_emits_bounded_reason_without_window_identity():
    events = []
    resolver = SafeWindowResolver(
        enumerate_windows=lambda: [],
        inspect_window=lambda _hwnd: None,
        diagnose_windows=lambda: {
            "window_failure_reason": "platform_title_not_matched",
            "supported_browser_windows": 1,
            "platform_title_windows": 0,
            "minimized_platform_windows": 0,
        },
    )
    updater = SceneContextUpdater(
        resolver=resolver,
        capture_backend=CaptureSequence([frame(40)]),
        event_sink=lambda event_type, **fields: events.append(
            {"event_type": event_type, **fields}
        ),
        profile_resolution_enabled=False,
    )

    assert updater.tick() is None
    assert events[-1]["window_status"] == "window_invalid"
    assert events[-1]["window_failure_reason"] == "platform_title_not_matched"
    assert events[-1]["supported_browser_windows"] == 1
    serialized = repr(events[-1])
    assert "hwnd" not in serialized
    assert "SECRET STREAM TITLE" not in serialized


def test_groq_provider_uses_single_attempt_qwen_and_bounded_diagnostics(
    real_groq,
):
    raw_response = MagicMock()
    raw_response.headers = {
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": "5519",
        "x-ratelimit-reset-tokens": "1m26.4s",
        "x-private-debug": "SECRET HEADER",
    }
    raw_response.parse.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="StarCraft\nSECRET RAW TEXT"),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=837,
            completion_tokens=4,
            total_tokens=841,
        ),
    )
    create = MagicMock(return_value=raw_response)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )
    )
    original_key = cfg.keys.groq
    object.__setattr__(cfg.keys, "groq", "test-key")
    try:
        with patch("groq.Groq", return_value=client) as constructor:
            result = scene_vision.GroqVisionProvider(
                prompt=_VISION_PROMPT
            ).classify(b"jpeg")
    finally:
        object.__setattr__(cfg.keys, "groq", original_key)

    assert isinstance(result, VisionClassification)
    assert result.text == "StarCraft\nSECRET RAW TEXT"
    assert result.diagnostics.outcome == "success"
    assert result.diagnostics.attempt_limit == 1
    assert result.diagnostics.provider == "groq"
    assert result.diagnostics.model == "qwen/qwen3.8-27b"
    assert result.diagnostics.prompt_tokens == 837
    assert result.diagnostics.completion_tokens == 4
    assert result.diagnostics.total_tokens == 841
    assert result.diagnostics.finish_reason == "stop"
    assert result.diagnostics.rate_limit_tpm == 8000
    assert result.diagnostics.rate_limit_remaining_tokens == 5519
    assert result.diagnostics.rate_limit_reset_tokens_sec == 86.4
    assert len(result.diagnostics.attempt_chain) == 1
    assert "SECRET" not in repr(result.diagnostics.event_fields())
    constructor.assert_called_once_with(
        api_key="test-key",
        timeout=20.0,
        max_retries=0,
    )
    create.assert_called_once()
    request = create.call_args.kwargs
    assert request["model"] == "qwen/qwen3.8-27b"
    assert request["reasoning_effort"] == "none"
    assert request["max_tokens"] == 96
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["content"][0]["text"] == _VISION_PROMPT
    assert request["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


def test_groq_provider_configuration_error_is_nonretryable(real_groq):
    provider = scene_vision.GroqVisionProvider(
        model_name="qwen/qwen3.6-27b",
        prompt="",
        api_key="test-key",
        timeout=20.0,
    )

    with pytest.raises(VisionProviderFailure) as captured:
        provider.classify(b"jpeg")

    assert captured.value.diagnostics.error_type == "configuration_error"
    assert captured.value.diagnostics.retryable is False


def test_groq_provider_timeout_is_bounded_and_does_not_leak_message(real_groq):
    create = MagicMock(
        side_effect=real_groq.APITimeoutError(
            request=httpx.Request("POST", "https://api.groq.com/secret")
        )
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )
    )
    original_key = cfg.keys.groq
    object.__setattr__(cfg.keys, "groq", "test-key")
    try:
        with patch("groq.Groq", return_value=client):
            with pytest.raises(VisionProviderFailure) as captured:
                scene_vision.GroqVisionProvider(
                    prompt=_VISION_PROMPT
                ).classify(b"jpeg")
    finally:
        object.__setattr__(cfg.keys, "groq", original_key)

    assert captured.value.diagnostics.outcome == "error"
    assert captured.value.diagnostics.attempt_limit == 1
    assert captured.value.diagnostics.error_type == "timeout"
    assert captured.value.diagnostics.retryable is True
    assert captured.value.diagnostics.provider == "groq"
    assert "api.groq.com" not in repr(captured.value.diagnostics.event_fields())
    create.assert_called_once()


def test_groq_rate_limit_diagnostics_drop_response_message_and_body(real_groq):
    response = httpx.Response(
        429,
        headers={
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "18.607s",
            "x-private-debug": "SECRET HEADER",
        },
        request=httpx.Request("POST", "https://api.groq.com/secret"),
    )
    error = real_groq.RateLimitError(
        "SECRET PROVIDER MESSAGE",
        response=response,
        body={"secret": "SECRET BODY"},
    )

    create = MagicMock(side_effect=error)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )
    )
    with patch("groq.Groq", return_value=client):
        with pytest.raises(VisionProviderFailure) as captured:
            scene_vision.GroqVisionProvider(
                model_name="qwen/qwen3.6-27b",
                prompt=_VISION_PROMPT,
                api_key="test-key",
                timeout=20.0,
            ).classify(b"jpeg")

    diagnostics = captured.value.diagnostics
    assert diagnostics.outcome == "error"
    assert diagnostics.error_type == "rate_limit"
    assert diagnostics.retryable is True
    assert diagnostics.http_status == 429
    assert diagnostics.rate_limit_tpm == 8000
    assert diagnostics.rate_limit_remaining_tokens == 0
    assert diagnostics.rate_limit_reset_tokens_sec == 18.607
    assert "SECRET" not in repr(diagnostics.event_fields())


def test_groq_http_408_is_retryable_timeout(real_groq):
    response = httpx.Response(
        408,
        request=httpx.Request("POST", "https://api.groq.com/secret"),
    )
    error = real_groq.APIStatusError(
        "SECRET PROVIDER MESSAGE",
        response=response,
        body={"secret": "SECRET BODY"},
    )
    create = MagicMock(side_effect=error)
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=create)
            )
        )
    )

    with patch("groq.Groq", return_value=client):
        with pytest.raises(VisionProviderFailure) as captured:
            scene_vision.GroqVisionProvider(
                model_name="qwen/qwen3.6-27b",
                prompt=_VISION_PROMPT,
                api_key="test-key",
                timeout=20.0,
            ).classify(b"jpeg")

    diagnostics = captured.value.diagnostics
    assert diagnostics.error_type == "timeout"
    assert diagnostics.retryable is True
    assert diagnostics.http_status == 408
    assert "SECRET" not in repr(diagnostics.event_fields())


def test_groq_provider_malformed_headers_fail_soft(real_groq):
    raw_response = MagicMock()
    raw_response.headers = {
        "x-ratelimit-limit-tokens": "999999999999",
        "x-ratelimit-remaining-tokens": "-1",
        "x-ratelimit-reset-tokens": "999999999h",
    }
    raw_response.parse.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="unknown"))],
        usage=None,
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=SimpleNamespace(
                    create=MagicMock(return_value=raw_response)
                )
            )
        )
    )
    original_key = cfg.keys.groq
    object.__setattr__(cfg.keys, "groq", "test-key")
    try:
        with patch("groq.Groq", return_value=client):
            result = scene_vision.GroqVisionProvider(
                prompt=_VISION_PROMPT
            ).classify(b"jpeg")
    finally:
        object.__setattr__(cfg.keys, "groq", original_key)

    assert result.text == "unknown"
    fields = result.diagnostics.event_fields()
    assert fields["vision_outcome"] == "success"
    assert fields["vision_attempt_limit"] == 1
    assert fields["vision_provider"] == "groq"
    assert fields["vision_model"] == "qwen/qwen3.8-27b"
    assert "vision_rate_limit_tpm" not in fields
    assert "vision_rate_limit_remaining_tokens" not in fields
    assert "vision_rate_limit_reset_tokens_sec" not in fields


def test_resolver_fails_closed_for_multiple_platform_windows():
    source = WindowSource([window(hwnd=1), window(hwnd=2)])
    resolver = SafeWindowResolver(
        enumerate_windows=source.enumerate,
        inspect_window=source.inspect,
    )

    result = resolver.resolve()

    assert result.status == "multiple_candidates"
    assert result.identity is None


def test_resolver_suspends_hidden_player_and_rejects_detectable_hwnd_reuse():
    source = WindowSource([window(hwnd=9, pid=101)])
    resolver = SafeWindowResolver(
        enumerate_windows=source.enumerate,
        inspect_window=source.inspect,
    )
    locked = resolver.resolve().identity
    generation = resolver.window_generation
    assert locked is not None

    source.candidates = [
        window(
            hwnd=9,
            pid=101,
            title="ChatGPT - Google Chrome",
            platform="",
        )
    ]
    assert resolver.validate(locked).status == "player_not_visible"
    assert resolver.window_generation == generation

    source.candidates = [window(hwnd=9, pid=202)]
    relocked = resolver.resolve().identity
    assert relocked is not None
    source.candidates = [window(hwnd=9, pid=303)]
    assert resolver.validate(relocked).status == "identity_changed"


def test_hidden_player_retains_profile_and_same_player_resumes_without_generation_churn():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    url = _profile_result("url", "url_member_moka", "url_brand_group")
    with patch.object(scene_context, "profile_state", state):
        updater, source, _capture, _provider, _manual, events, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=QuerySequence([url, url]),
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        updater.tick()
        clock.advance(1)
        updater.tick()
        confirmed_snapshot = state.current()
        confirmed_generation = state.current().generation
        window_generation = updater._resolver.window_generation
        source.candidates = [window(title="ChatGPT - Google Chrome", platform="")]
        clock.advance(29)
        updater.tick()
        clock.advance(60 * 10)
        updater.tick()
        hidden_snapshot = state.current()
        assert hidden_snapshot.content_profile_id == "url"
        assert hidden_snapshot.effective_profile_id == "url"
        assert hidden_snapshot is confirmed_snapshot
        assert hidden_snapshot.cache_identity == confirmed_snapshot.cache_identity
        assert hidden_snapshot.generation == confirmed_generation
        assert updater._resolver.window_generation == window_generation

        source.candidates = [window()]
        clock.advance(30)
        updater.tick()

    assert state.current().effective_profile_id == "url"
    assert state.current() is confirmed_snapshot
    assert state.current().generation == confirmed_generation
    assert updater._resolver.window_generation == window_generation
    suspended = next(
        event for event in events
        if event.get("event_type") == "profile_resolution"
        and event.get("status") == "suspended"
    )
    assert suspended["reason"] == "player_not_visible"
    assert suspended["activation_decision"] == "retain_confirmed_profile"


def test_hidden_player_can_resume_to_different_supported_player_and_switch_profile():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    answers = QuerySequence([
        _profile_result("url", "url_member_moka", "url_brand_group"),
        _profile_result("url", "url_member_moka", "url_brand_group"),
        _profile_result(
            "hades_chxxnnx", "hades_member_chaenna", "hades_brand_group"
        ),
        _profile_result(
            "hades_chxxnnx", "hades_member_chaenna", "hades_brand_group"
        ),
    ])
    with patch.object(scene_context, "profile_state", state):
        updater, source, _capture, _provider, _manual, _events, clock = make_updater(
            frames=[frame(40), frame(80), frame(120), frame(160)],
            profile_vision_provider=answers,
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        updater.tick()
        clock.advance(1)
        updater.tick()
        url_generation = state.current().generation
        source.candidates = [window(title="ChatGPT - Google Chrome", platform="")]
        clock.advance(60)
        updater.tick()
        assert state.current().generation == url_generation

        source.candidates = [
            window(title="Different stream - CHZZK - Google Chrome", platform="chzzk")
        ]
        clock.advance(1)
        updater.tick()
        clock.advance(1)
        updater.tick()

    assert state.current().effective_profile_id == "hades_chxxnnx"
    assert state.current().generation == url_generation + 1


def test_production_timing_replay_retains_url_during_hidden_tab_interval():
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    url = _profile_result("url", "url_member_moka", "url_brand_group")
    with patch.object(scene_context, "profile_state", state):
        updater, source, _capture, _provider, _manual, _events, clock = make_updater(
            frames=[frame(40), frame(80)],
            profile_vision_provider=QuerySequence([url, url]),
            profile_resolution_enabled=True,
        )
        updater._profile_fast_gap = 0
        clock.advance(13)
        updater.tick()  # 11:50:13 URL candidate
        clock.advance(1)
        updater.tick()  # 11:50:14 URL confirmed
        generation = state.current().generation
        source.candidates = [window(title="ChatGPT - Google Chrome", platform="")]
        clock.advance(11)
        updater.tick()  # 11:50:24 observation suspended
        clock.advance(18)
        assert state.current().effective_profile_id == "url"  # 11:50:42
        clock.advance(11)
        assert state.current().effective_profile_id == "url"  # 11:50:53
        source.candidates = [window()]
        clock.advance(31)
        updater.tick()  # 11:51:24 same URL reconfirmed

    assert state.current().effective_profile_id == "url"
    assert state.current().generation == generation


def test_same_hwnd_platform_title_change_rotates_window_generation():
    source = WindowSource([window(title="Stream A - SOOP - Google Chrome")])
    resolver = SafeWindowResolver(
        enumerate_windows=source.enumerate,
        inspect_window=source.inspect,
    )
    first = resolver.resolve().identity
    generation = resolver.window_generation
    assert first is not None

    source.candidates = [
        window(title="Stream B - SOOP - Google Chrome")
    ]
    result = resolver.validate(first)

    assert result.status == "title_changed"
    assert result.title_changed is True
    assert resolver.window_generation > generation


def test_print_window_failure_has_no_bbox_or_fullscreen_fallback():
    backend = PrintWindowCaptureBackend()
    with patch.object(scene_context, "_print_window_image", return_value=None):
        result = backend.capture(window())

    assert result.status == "capture_unavailable"
    assert result.jpeg == b""


def test_safe_crop_rejects_black_and_accepts_nontrivial_frame():
    black = scene_context._prepare_capture(Image.new("RGB", (800, 600), "black"))
    patterned_image = Image.new("RGB", (800, 600), "white")
    for x in range(0, 800, 40):
        for y in range(0, 600, 40):
            if (x // 40 + y // 40) % 2:
                for px in range(x, min(x + 40, 800)):
                    for py in range(y, min(y + 40, 600)):
                        patterned_image.putpixel((px, py), (20, 40, 80))
    patterned = scene_context._prepare_capture(patterned_image)

    assert black.status == "capture_low_quality"
    assert black.frame_quality == "black"
    assert patterned.status == "ok"
    assert patterned.content_crop is True
    assert patterned.jpeg


def test_scene_observer_confirms_profile_identity_without_activity_vision():
    # Activity detection is gone (off since 2026-10-11, removed 2026-11-10), so
    # the Groq vision quota goes to profile identity alone.
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"솜망"}'])
    with patch.object(scene_context, "profile_state", state):
        updater, _source, _capture, provider, *_rest = make_updater(
            frames=[image_frame_with_identity_block(30)],
            profile_resolution_enabled=True,
            profile_vision_provider=QuerySequence([]),
            identity_roi_provider=identity_reader,
            identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
        )
        updater.tick()
    assert state.current().effective_profile_id == "url"
    assert len(identity_reader.calls) == 1


def test_post_capture_wrong_tab_or_second_candidate_never_reaches_profile_reader():
    # Privacy boundary: if the tab or the candidate set changes during
    # PrintWindow, the captured frame must not reach any vision provider.
    for mutate, expected in (
        (
            lambda source: setattr(
                source,
                "candidates",
                [window(title="ChatGPT - Google Chrome", platform="")],
            ),
            "player_not_visible",
        ),
        (
            lambda source: setattr(
                source,
                "candidates",
                [window(hwnd=100), window(hwnd=200, pid=20)],
            ),
            "multiple_candidates",
        ),
    ):
        holder = {}

        def after_capture():
            mutate(holder["source"])

        state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
        identity_reader = QuerySequence(['{"identity":"솜망"}'])
        scene_reader = QuerySequence(['{"identity":"솜망"}'])
        with patch.object(scene_context, "profile_state", state):
            updater, source, _capture, _provider, _manual, events, _clock = make_updater(
                frames=[image_frame_with_identity_block(30)],
                profile_resolution_enabled=True,
                profile_vision_provider=scene_reader,
                identity_roi_provider=identity_reader,
                identity_roi_store=FixedRoiStore(NormalizedRoi(0, 0, 0.5, 1)),
                after_capture=after_capture,
            )
            holder["source"] = source
            assert updater.tick() is None

        assert identity_reader.calls == []
        assert scene_reader.calls == []
        shadow = [e for e in events if e["event_type"] == "activity_shadow"]
        assert shadow[-1]["validation_stage"] == "post_capture"
        assert shadow[-1]["discard_reason"] == expected


def test_manual_activity_pause_and_stop_keep_their_events_and_generations():
    manual = {"value": ""}
    updater, _source, _capture, _provider, _manual, events, _clock = make_updater(
        manual=manual,
    )
    manual["value"] = "StarCraft"
    updater.tick()
    publication = [e for e in events if e["event_type"] == "activity_publication"]
    assert publication[-1]["action"] == "manual_override"
    assert publication[-1]["effective_source"] == "manual"
    assert publication[-1]["automatic_available"] is False

    generation = updater.resolver_generation
    updater.set_paused(True)
    assert updater.resolver_generation == generation + 1
    assert updater.tick() is None
    updater.set_paused(False)
    updater.stop()
    assert updater.resolver_generation == generation + 3

    manual["value"] = ""
    updater._sync_manual_activity()
    publication = [e for e in events if e["event_type"] == "activity_publication"]
    assert publication[-1]["action"] == "cleared"


def test_tick_syncs_lifecycle_at_end_of_tick():
    updater, *_rest = make_updater()
    calls = []
    original_lifecycle, original_manual = updater._sync_lifecycle, updater._sync_manual_activity
    updater._sync_lifecycle = lambda: (calls.append("lifecycle"), original_lifecycle())[1]
    updater._sync_manual_activity = lambda: (calls.append("manual"), original_manual())[1]
    updater.tick()
    assert calls[-2:] == ["lifecycle", "manual"]


def test_animated_name_block_is_not_reread_at_fast_cadence():
    # Run 20261011T141844Z-31580: the confirmed 랑코_ block kept "changing" and was
    # re-read every ~6 s until Groq 429. One same-identity re-read marks it noisy.
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"솜망"}'])
    frames = [image_frame_with_identity_block(v) for v in (30, 90, 150, 210, 30, 90)]
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = _confirmed_roi_updater(state, identity_reader, frames)
        updater.tick()  # t=0 initial read
        assert len(identity_reader.calls) == 1
        clock.advance(20)
        updater.tick()  # t=20 first pixel-only change: read at once, same identity
        assert len(identity_reader.calls) == 2
        clock.advance(20)
        updater.tick()  # t=40 changes again within the noisy gap: no read
        clock.advance(20)
        updater.tick()  # t=60 still within 60 s of the t=20 re-read
        assert len(identity_reader.calls) == 2
        clock.advance(25)
        updater.tick()  # t=85 gap elapsed: read again
    assert len(identity_reader.calls) == 3
    assert state.current().effective_profile_id == "url"


def test_real_name_change_on_a_noisy_block_is_read_after_the_gap():
    # The trade-off: on a block already shown to be animated, a genuine name change
    # without a title/window change is read when the 60 s gap ends.
    state = ProfileState(profile_state.registry, source_profile_id="isegye_lilpa")
    identity_reader = QuerySequence(['{"identity":"솜망"}', '{"identity":"솜망"}', '{"identity":"숨주먹"}'])
    frames = [image_frame_with_identity_block(v) for v in (30, 90, 220, 220)]
    with patch.object(scene_context, "profile_state", state):
        updater, *_rest, clock = _confirmed_roi_updater(state, identity_reader, frames)
        updater.tick()
        clock.advance(20)
        updater.tick()  # noisy re-read, still 솜망 (url)
        clock.advance(20)
        updater.tick()  # name actually changed, but within the noisy gap
        assert state.current().effective_profile_id == "url"
        clock.advance(45)
        updater.tick()  # gap elapsed
    assert state.current().effective_profile_id == "hades_chxxnnx"


def test_noisy_reread_gap_config_must_be_finite_and_non_negative():
    from config import _Scene

    assert _Scene(profile_identity_noisy_reread_gap_sec=0).profile_identity_noisy_reread_gap_sec == 0
    for bad in (float("inf"), float("nan"), -1.0, True):
        with pytest.raises(ValueError, match="noisy_reread_gap_sec"):
            _Scene(profile_identity_noisy_reread_gap_sec=bad)
