"""Explicit request/result ownership across provider and fallback boundaries."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error

from config import cfg
from modules.provisional_subtitles import provisional_fingerprint
from modules.translation_engines import (
    DeepSeekTranslationEngine, EngineResult, GroqTranslationEngine, NvidiaEngine,
    call_engine_with_deadline,
)
from modules.translation_request import TranslationRequest, freeze_route_request
from modules.translation_runtime import FallbackState, call_with_fallback
from modules.translation_runtime import cache_key
from modules.translator import Translator


class _Response:
    def __init__(self, fingerprint: str):
        self.data = json.dumps({
            "choices": [{"message": {"content": "譯文"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                      "prompt_cache_hit_tokens": 0,
                      "prompt_cache_miss_tokens": 7},
            "system_fingerprint": fingerprint,
        }).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.data


def _request(route):
    return TranslationRequest(
        original_source="안녕", provider_source="안녕", incomplete=False,
        history=(), history_cohort=("session", "", 0),
        profile_cache_identity="", activity_cache_identity="",
        request_protection_identity="", canonical_obligations=(), routes=(route,),
    )


def test_temperature_changes_contract_and_provisional_identity_but_frozen_body_stays_fixed():
    engine = DeepSeekTranslationEngine()
    engine._api_key = "mock-only"
    source = "안녕"
    prompt = "Translate to Traditional Chinese."
    baseline = cfg.translation.deepseek_temperature
    route = freeze_route_request(engine, source, prompt, False, [])
    changed_cfg = SimpleNamespace(
        translation=replace(cfg.translation, deepseek_temperature=baseline + 0.2)
    )
    with patch("modules.translation_request.cfg", changed_cfg):
        changed = freeze_route_request(engine, source, prompt, False, [])
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request.data)
            return _Response("frozen-fingerprint")

        with patch("urllib.request.urlopen", side_effect=urlopen):
            result = engine.translate_messages_result(route)
    assert sent == [route.body]
    assert isinstance(result, EngineResult)
    assert result.text == "譯文"
    assert result.system_fingerprint == "frozen-fingerprint"
    assert result.finish_reason == "stop"
    assert result.usage_dict()["prompt"] == 7
    assert _request(route).request_id != _request(changed).request_id
    translator = Translator.__new__(Translator)
    translator._history_cohort = lambda: ("session", "", 0)
    translator._history_session = lambda: "session"
    first_prompt_version = translator._prompt_version_for_engine(
        engine, prompt, route_request=route
    )
    changed_prompt_version = translator._prompt_version_for_engine(
        engine, prompt, route_request=changed
    )
    assert cache_key(source, False, first_prompt_version, "deepseek", engine.model_name) != (
        cache_key(source, False, changed_prompt_version, "deepseek", engine.model_name)
    )
    common = dict(
        prepared_source=source, source_utterance_ids=("u1",),
        evidence_source_utterance_ids=("u1",), profile_id="",
        activity_cache_identity="", history_cohort=("session", "", 0),
        messages=route.messages, incomplete=False,
    )
    assert provisional_fingerprint(
        **common, provider_options=route.options_dict(),
        timeout_seconds=route.timeout_seconds,
    ) != provisional_fingerprint(
        **common, provider_options=changed.options_dict(),
        timeout_seconds=changed.timeout_seconds,
    )


def test_prompt_cache_identity_uses_frozen_request_cohort():
    engine = DeepSeekTranslationEngine()
    prompt = "Translate to Traditional Chinese."
    route = freeze_route_request(engine, "source", prompt, False, [])
    request = _request(route)
    assert request.request_id != replace(
        request, history_context_enabled=True
    ).request_id
    translator = Translator.__new__(Translator)
    translator._history_cohort = lambda: ("other-session", "changed", 9)
    translator._history_session = lambda: "other-session"
    frozen_version = translator._prompt_version_for_engine(
        engine, prompt, route_request=route, translation_request=request,
    )
    translator._history_cohort = lambda: ("third-session", "changed-again", 10)
    translator._history_session = lambda: "third-session"
    assert translator._prompt_version_for_engine(
        engine, prompt, route_request=route, translation_request=request,
    ) == frozen_version
    for context_window in (0, 1):
        replacement_cfg = SimpleNamespace(
            translation=replace(cfg.translation, context_window=context_window),
            scene=cfg.scene,
        )
        with patch("modules.translator.cfg", replacement_cfg):
            assert translator._prompt_version_for_engine(
                engine, prompt, route_request=route, translation_request=request,
            ) == frozen_version


def test_nvidia_transient_retry_policy_is_frozen_with_request():
    engine = NvidiaEngine()
    engine._api_key = "mock-only"
    engine._retry_transient_errors = True
    enabled = freeze_route_request(engine, "source", "Translate.", False, [])
    engine._retry_transient_errors = False
    disabled = freeze_route_request(engine, "source", "Translate.", False, [])
    assert enabled.body == disabled.body
    assert _request(enabled).request_id != _request(disabled).request_id
    with patch("urllib.request.urlopen", side_effect=[
        urllib.error.URLError("synthetic network failure"), _Response("retry-ok"),
    ]) as send, patch("modules.translation_engines.time.sleep"):
        result = engine.translate("source", "Translate.", False, [],
                                  route_request=enabled)
    assert result.text
    assert result.system_fingerprint == "retry-ok"
    assert send.call_count == 2
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(
        "synthetic network failure"
    )) as send:
        result = engine.translate("source", "Translate.", False, [],
                                  route_request=disabled)
    assert result.text is None
    assert send.call_count == 1


def test_deadline_result_is_owned_by_timed_out_call():
    engine = DeepSeekTranslationEngine()
    engine._api_key = "mock-only"
    route = freeze_route_request(engine, "안녕", "Translate.", False, [])
    release = threading.Event()
    entered = threading.Event()

    def blocked_urlopen(request, timeout=None):
        entered.set()
        release.wait(timeout=2)
        return _Response("late-fingerprint")

    with patch("urllib.request.urlopen", side_effect=blocked_urlopen):
        result = call_engine_with_deadline(
            engine, "안녕", "Translate.", False, [],
            timeout_seconds=0.02, max_inflight=1,
            messages_frozen=route.messages, route_request=route,
        )
        assert entered.is_set()
        assert isinstance(result, EngineResult)
        assert result.text is None
        assert result.deadline_exceeded
        assert result.system_fingerprint == ""
        assert result.cost_usd is None
        release.set()


def test_deadline_wrapper_keeps_legacy_adapter_string_result():
    class LegacyAdapter:
        engine_name = "custom"
        model_name = "fixture"

        def translate_result(self, *_args, **_kwargs):
            return "translated"

    result = call_engine_with_deadline(
        LegacyAdapter(), "source", "Translate.", False, [],
        timeout_seconds=0.5, max_inflight=1,
        route_request=SimpleNamespace(body=b"fixture"),
    )
    assert isinstance(result, EngineResult)
    assert result.text == "translated"
    assert result.cost_usd is None


def test_fallback_keeps_failed_primary_separate_from_selected_result():
    deepseek = DeepSeekTranslationEngine()
    groq = GroqTranslationEngine()
    deepseek._api_key = groq._api_key = "mock-only"
    routes = {
        engine.engine_name: freeze_route_request(engine, "안녕", "Translate.", False, [])
        for engine in (deepseek, groq)
    }

    def urlopen(request, timeout=None):
        if "deepseek" in request.full_url:
            raise urllib.error.URLError("synthetic failure")
        return _Response("selected-groq")

    with patch("urllib.request.urlopen", side_effect=urlopen):
        text, idx, details = call_with_fallback(
            [deepseek, groq], FallbackState(), "안녕", "Translate.", False, [],
            2, lambda _candidate, _source: False, Mock(),
            frozen_messages_by_engine={k: v.messages for k, v in routes.items()},
            route_requests_by_engine=routes, return_details=True,
        )
    assert text == "譯文" and idx == 1
    assert len(details.attempts) == 2
    assert details.attempts[0]["system_fingerprint"] == ""
    assert details.selected_attempt["system_fingerprint"] == "selected-groq"
    assert details.selected_attempt["token_prompt"] == 7
    assert details.selected_attempt["provider_options"] == routes["groq"].options_dict()


def test_final_translation_event_fields_include_selected_options_and_fingerprint():
    translator = Translator()
    deepseek = DeepSeekTranslationEngine()
    deepseek._api_key = "mock-only"
    translator._engines = [deepseek]
    with patch("urllib.request.urlopen", return_value=_Response("final-selected")):
        outcome = translator.translate_event("안녕하세요 오늘 방송이에요")
    fields = outcome.as_event_fields(1.0, {})
    assert outcome.status == "success"
    assert fields["provider_options"]["temperature"] == cfg.translation.deepseek_temperature
    assert fields["system_fingerprint"] == "final-selected"
