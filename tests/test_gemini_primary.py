"""Mock-only coverage of the pluggable protected primary provider."""
import io
import json
import logging
import queue
import threading
import time
from dataclasses import replace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from config import cfg
from modules import translation_engines as engines
from modules import translation_request as requests
from modules import translator as tr
from modules import provisional_subtitles as provisional
from modules.translation_runtime import FallbackState, call_with_fallback, probe_primary_recovery
from scripts.replay_translation_contract import reconstruct


@pytest.fixture
def provider_config(monkeypatch):
    def configure(provider="gemini", key="mock-only", **settings):
        selected = replace(cfg, keys=replace(cfg.keys, gemini=key, deepseek="mock-only", groq_fallback="mock-only"),
                           translation=replace(cfg.translation, primary_provider=provider,
                                               deepseek_route="primary", translation_mode="live", **settings))
        for module in (engines, requests, tr, provisional):
            monkeypatch.setattr(module, "cfg", selected)
        return selected
    configure()
    return configure


class Response:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self):
        return self.data


def answer(reason="STOP", text="subtitle"):
    return {"candidates": [{"finishReason": reason, "content": {"parts": [
        {"text": "hidden reasoning", "thought": True}, {"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10,
                          "thoughtsTokenCount": 20, "totalTokenCount": 130}}


def route(engine):
    return requests.freeze_route_request(engine, "hello", "translate", False, [])


@pytest.mark.parametrize("mode", ["live", "clip"])
@pytest.mark.parametrize("provider,key,expected", [
    ("gemini", "mock-only", ("gemini", "deepseek", "groq")),
    ("gemini", "", ("deepseek", "groq")),
    ("deepseek", "mock-only", ("deepseek", "groq")),
])
def test_chain_order_and_missing_key(provider_config, mode, provider, key, expected):
    selected = provider_config(provider, key)
    object.__setattr__(selected.translation, "translation_mode", mode)
    assert engines.effective_engine_chain_names() == expected
    assert engines.make_primary_engine().engine_name == expected[0]
    assert tuple(e.engine_name for e in engines._build_engine_chain()) == expected
    object.__setattr__(selected.translation, "deepseek_route", "off")
    assert engines.effective_engine_chain_names() == ("groq",)
    assert not provisional.primary_provisional_eligible()


def test_gemini_wire_contract_and_thinking_cost(provider_config):
    engine = engines.GeminiTranslationEngine()
    messages = (("system", "one"), ("system", "two"), ("user", "source"), ("assistant", "target"))
    frozen = requests.primary_route_for_messages(engine, messages)
    body = json.loads(frozen.body)
    assert body == {"systemInstruction": {"parts": [{"text": "one\n\ntwo"}]},
        "contents": [{"role": "user", "parts": [{"text": "source"}]},
                     {"role": "model", "parts": [{"text": "target"}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": engine._max_tokens + 512,
                             "thinkingConfig": {"thinkingLevel": "low"}}}
    with patch("urllib.request.urlopen", return_value=Response(answer(text=" a "))) as http:
        result = engine.translate_messages_result(frozen)
    request = http.call_args.args[0]
    assert request.full_url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    assert request.get_method() == "POST"
    assert request.get_header("X-goog-api-key") == "mock-only"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == frozen.body
    assert http.call_args.kwargs["timeout"] == 3.0
    assert result.text == "a"
    assert result.finish_reason == "STOP"
    assert result.usage_dict()["output"] == 30
    assert result.usage_dict()["prompt"] == 100
    assert result.usage_dict()["total"] == 130
    assert result.cost_usd == pytest.approx((100 * .75 + 30 * 3.75) / 1_000_000)
    assert result.diagnostics_dict()["api_pricing_revision"] == "2026-10-12"
    assert http.call_count == 1


CONTENT_CASES = [answer(reason) for reason in ("MAX_TOKENS", "SAFETY", "RECITATION", "OTHER")]
CONTENT_CASES += [answer(text="  "), {"candidates": []}, {}]
ERROR_CASES = [TimeoutError(), URLError("connection refused"),
    HTTPError("mock", 429, "rate limit", {}, io.BytesIO()),
    HTTPError("mock", 503, "unavailable", {}, io.BytesIO()),
    b"invalid-json", {"candidates": "invalid"}, {"candidates": [{"content": None}]},
    {"candidates": [{"content": {"parts": [{"text": 42}]}}]}]


@pytest.mark.parametrize("payload", CONTENT_CASES + ERROR_CASES)
def test_n3_final_and_probe_classification(provider_config, payload):
    primary = engines.GeminiTranslationEngine()
    fallback = engines.DeepSeekTranslationEngine()
    routes = {e.engine_name: route(e) for e in (primary, fallback)}
    fallback_response = {"choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}]}
    is_content = any(payload is row for row in CONTENT_CASES)
    def http(request, **_):
        if "deepseek" in request.full_url:
            return Response(fallback_response)
        if isinstance(payload, Exception):
            raise payload
        return Response(payload)
    state = FallbackState()
    with patch("urllib.request.urlopen", side_effect=http) as transport:
        text, index, detail = call_with_fallback(
            [primary, fallback], state, "hello", "translate", False, [], 1,
            lambda *_: False, logging.getLogger("test"), circuit_breaker_enabled=True,
            recovery_cooldown_seconds=10, route_requests_by_engine=routes, return_details=True)
    assert (text, index) == ("fallback", 1)
    assert transport.call_count == 2
    assert detail.attempts[0]["failure_scope"] == ("content" if is_content else "provider")
    assert state.active_idx == (0 if is_content else 1)
    if isinstance(payload, HTTPError) and payload.code == 429:
        assert detail.attempts[0]["api_error_type"] == "api_error"
        assert detail.attempts[0]["api_error_message_class"] == "rate_limit"
    if is_content and payload.get("candidates"):
        assert detail.attempts[0]["finish_reason"] == payload["candidates"][0]["finishReason"]
    state = FallbackState(active_idx=1, primary_cooldown_until=5, consecutive_probe_successes=1)
    observations = []
    with patch("urllib.request.urlopen", side_effect=http) as transport:
        recovered = probe_primary_recovery(
            [primary, fallback], state, "hello", "translate", lambda *_: False,
            logging.getLogger("test"), circuit_breaker_enabled=True,
            recovery_cooldown_seconds=10, clock=lambda: 20, observation_sink=observations.append)
    assert not recovered
    assert transport.call_count == 1
    assert state.consecutive_probe_successes == (1 if is_content else 0)
    assert state.primary_cooldown_until == (5 if is_content else 30)


def test_probe_success_runs_gemini_and_recovers(provider_config):
    primary = engines.GeminiTranslationEngine()
    state = FallbackState(active_idx=1)
    with patch("urllib.request.urlopen", return_value=Response(answer())) as http:
        assert probe_primary_recovery([primary, engines.DeepSeekTranslationEngine()], state,
            "hello", "translate", lambda *_: False, logging.getLogger("test"))
    assert "generativelanguage" in http.call_args.args[0].full_url
    assert state.active_idx == 0


def test_gemini_hard_deadline_soft_falls_to_deepseek(provider_config):
    provider_config(gemini_timeout=.02)
    primary, fallback = engines.GeminiTranslationEngine(), engines.DeepSeekTranslationEngine()
    finished = threading.Event()
    def http(request, **_):
        if "generativelanguage" in request.full_url:
            time.sleep(.08)
            finished.set()
            return Response(answer())
        return Response({"choices": [{"message": {"content": "fallback"}}]})
    with patch("urllib.request.urlopen", side_effect=http):
        text, index, detail = call_with_fallback([primary, fallback], FallbackState(),
            "hello", "translate", False, [], 2, lambda *_: False, logging.getLogger("test"),
            deadline_at=time.monotonic() + 1,
            route_requests_by_engine={e.engine_name: route(e) for e in (primary, fallback)}, return_details=True)
        assert (text, index) == ("fallback", 1)
        assert detail.attempts[0]["deadline_scope"] == "route"
        assert finished.wait(1)


@pytest.mark.parametrize("field,value", [
    ("primary_provider", "deepseek"), ("gemini_model", "alternate"),
    ("gemini_thinking_level", "medium"), ("gemini_timeout", 4.0),
    ("gemini_temperature", .2), ("gemini_output_reserve_tokens", 600),
    ("gemini_input_usd_per_million", 1.0), ("gemini_output_usd_per_million", 4.0),
    ("gemini_pricing_revision", "next"),
])
def test_each_setting_rebuilds_chain_and_resets_fallback(provider_config, field, value):
    selected = provider_config()
    translator = tr.Translator()
    translator._fallback_state_obj.active_idx = 1
    translator._fallback_state_obj.consecutive_primary_failures = 3
    translator._fallback_state_obj.primary_cooldown_until = 100
    translator._fallback_state_obj.consecutive_probe_successes = 1
    original_engines = translator._engines
    original_key = engines.engine_chain_config_key()
    object.__setattr__(selected.translation, field, value)
    assert original_key != engines.engine_chain_config_key()
    translator._refresh_engines_if_needed()
    assert translator._engines is not original_engines
    state = translator._fallback_state_obj
    assert (state.active_idx, state.consecutive_primary_failures, state.primary_cooldown_until,
            state.consecutive_probe_successes) == (0, 0, 0.0, 0)
    assert state.chain_key == engines.engine_chain_config_key()
    assert state.chain_generation == 1


def _stale_result(shared):
    before = tr._copy_fallback_state(shared.fallback)
    after = tr._copy_fallback_state(before)
    after.active_idx = 2
    after.consecutive_primary_failures = 2
    return before, after


def test_in_flight_request_from_old_chain_cannot_write_into_rebuilt_chain(provider_config):
    selected = provider_config()
    shared = tr._new_translator_shared_state()
    worker_a, worker_b = tr.Translator(shared), tr.Translator(shared)
    assert len(worker_a._engines) == 3
    before, after = _stale_result(shared)  # worker A is in flight on gemini -> deepseek -> groq
    object.__setattr__(selected.translation, "primary_provider", "deepseek")
    worker_b._refresh_engines_if_needed()
    assert len(worker_b._engines) == 2
    with shared.lock:
        tr._merge_fallback_state(shared.fallback, before, after)
    assert shared.fallback.active_idx == 0
    # Worker A's own refresh must not reset the state worker B already moved on.
    shared.fallback.consecutive_primary_failures = 1
    worker_a._refresh_engines_if_needed()
    assert shared.fallback.consecutive_primary_failures == 1
    assert worker_a._engines_generation == worker_b._engines_generation


def test_switch_back_to_same_provider_still_drops_stale_results(provider_config):
    selected = provider_config()
    shared = tr._new_translator_shared_state()
    worker = tr.Translator(shared)
    before, after = _stale_result(shared)
    object.__setattr__(selected.translation, "primary_provider", "deepseek")
    worker._refresh_engines_if_needed()
    object.__setattr__(selected.translation, "primary_provider", "gemini")
    worker._refresh_engines_if_needed()
    assert shared.fallback.chain_key == before.chain_key
    with shared.lock:
        tr._merge_fallback_state(shared.fallback, before, after)
    assert shared.fallback.active_idx == 0


def test_worker_created_after_switch_takes_over_shared_state(provider_config):
    selected = provider_config()
    shared = tr._new_translator_shared_state()
    tr.Translator(shared)
    shared.fallback.active_idx = 2
    object.__setattr__(selected.translation, "primary_provider", "deepseek")
    late = tr.Translator(shared)
    assert shared.fallback.active_idx == 0
    assert shared.fallback.chain_generation == late._engines_generation
    before = tr._copy_fallback_state(shared.fallback)
    after = tr._copy_fallback_state(before)
    after.active_idx = 1
    tr._merge_fallback_state(shared.fallback, before, after)
    assert shared.fallback.active_idx == 1


def test_chain_and_key_come_from_one_config_read(provider_config, monkeypatch):
    selected = provider_config()
    built = []
    real_build = tr._build_engine_chain

    def build_then_switch():
        chain = real_build()
        if not built:
            object.__setattr__(selected.translation, "primary_provider", "deepseek")
        built.append(len(chain))
        return chain

    monkeypatch.setattr(tr, "_build_engine_chain", build_then_switch)
    chain, key = tr._consistent_engine_chain()
    assert built == [3, 2]
    assert len(chain) == 2 and key == engines.engine_chain_config_key()


def test_merge_on_same_chain_still_applies(provider_config):
    provider_config()
    shared = tr._new_translator_shared_state()
    before = tr._copy_fallback_state(shared.fallback)
    after = tr._copy_fallback_state(before)
    after.active_idx = 1
    tr._merge_fallback_state(shared.fallback, before, after)
    assert shared.fallback.active_idx == 1


def test_missing_key_rebuild_and_warning(provider_config, caplog):
    selected = provider_config()
    translator = tr.Translator()
    object.__setattr__(selected, "keys", replace(selected.keys, gemini=""))
    with caplog.at_level(logging.WARNING):
        translator._refresh_engines_if_needed()
    assert translator._engines[0].engine_name == "deepseek"
    assert "GEMINI_API_KEY missing" in caplog.text


@pytest.mark.parametrize("settings", [
    {"primary_provider": "unknown"}, {"gemini_timeout": 0}, {"gemini_timeout": float("nan")},
    {"gemini_temperature": True}, {"gemini_temperature": 3}, {"gemini_output_reserve_tokens": 0},
    {"gemini_thinking_level": "minimal"}, {"gemini_model": ""},
])
def test_invalid_config_rejected(settings):
    with pytest.raises(ValueError):
        replace(cfg.translation, **settings)


def test_provider_contract_cache_identity_and_replay(provider_config):
    translator = tr.Translator()
    context = tr._resolve_entity_request_context("hello")
    obligations = tr._canonical_obligations_for_request(context)
    protection = tr._request_protection_for("hello", context, obligations)
    prompt = translator._build_system_prompt(context.capsule)
    events = []
    def emit(event_type, _, **fields):
        events.append({"event_type": event_type, **fields})
    with patch.object(tr.runtime_events, "emit_once", side_effect=emit), patch("urllib.request.urlopen", return_value=Response(answer())):
        translator._call_with_fallback(protection, prompt, False, [], canonical_obligations=obligations)
    assert [row["engine"] for row in events] == ["gemini", "deepseek", "groq"]
    report = reconstruct(events, events[0]["translation_request_id"])
    assert report["differences"] == []
    events[0]["body_sha256"] = "tampered"
    assert any(row["field"] == "body_sha256" for row in reconstruct(events, events[0]["translation_request_id"])["differences"])
    from modules.translation_runtime import cache_key
    ds, gm = route(engines.DeepSeekTranslationEngine()), route(engines.GeminiTranslationEngine())
    assert ds.messages == gm.messages
    assert ds.identity_payload() != gm.identity_payload()
    assert cache_key("hello", False, "prompt", ds.engine, ds.model) != cache_key("hello", False, "prompt", gm.engine, gm.model)


@pytest.mark.parametrize("target,source,reason", [
    ("こんにちは", "hello", "raw_unexpected_japanese"),
    ("안녕하세요", "hello", "raw_unexpected_hangul"),
])
def test_gemini_raw_guard_rejection_and_evidence(provider_config, target, source, reason):
    guard = tr._translation_output_guard(engines.GeminiTranslationEngine(), target, source)
    assert guard["reason"] == reason.removeprefix("raw_")
    assert any(row["reason"] == reason for row in guard["failed_invariants"])


def test_gemini_guard_accepted_evidence_gate(provider_config):
    guard = tr._translation_output_guard(engines.GeminiTranslationEngine(), "你好", "hello")
    assert guard
    assert not guard.get("reason")
    assert guard["failed_invariants"] == []


def test_explicit_guard_flag_overrides_active_index_and_provider_name(provider_config):
    engine = engines.GeminiTranslationEngine()
    engine.protected_primary_route = False
    guard = tr._translation_output_guard(engine, "你好", "hello")
    assert guard == {}


def _preview(translator, *, switch_at_guard=False):
    from modules.activity_context import capture_activity_snapshot
    from modules.profile_context import profile_state
    store, subtitles = provisional.ProvisionalStore(), queue.Queue()
    worker = tr._ProvisionalWorker(translator._shared_state, store, threading.Event(), subtitles)
    snapshot = capture_activity_snapshot("chatting", source="manual")
    profile = profile_state.current()
    request = provisional.ProvisionalRequest(
        provisional_id="preview:gemini", text="오늘 방송 재미있어요", incomplete=True,
        profile_id=profile.effective_profile_id, source_utterance_ids=("utterance",),
        evidence_source_utterance_ids=("utterance",), activity_snapshot=snapshot,
        profile_snapshot=profile, requested_at_monotonic=time.monotonic(),
        first_stt_ready_at_monotonic=time.monotonic())
    contracts = []
    def emit(event_type, _, **fields):
        if event_type == "translation_request_contract":
            contracts.append({"event_type": event_type, **fields})
    original_guard = tr._translation_output_guard
    def guard(*args, **kwargs):
        result = original_guard(*args, **kwargs)
        if switch_at_guard:
            object.__setattr__(tr.cfg.translation, "primary_provider", "deepseek")
        return result
    with patch.object(tr.runtime_events, "emit_once", side_effect=emit), patch.object(
        tr, "_translation_output_guard", side_effect=guard), patch("urllib.request.urlopen", return_value=Response(answer(text="今天直播很有趣"))) as http:
        worker.translate(request)
    assert http.call_count == 1
    assert "generativelanguage" in http.call_args.args[0].full_url
    assert http.call_args.kwargs["timeout"] == 3.0
    assert contracts[0]["engine"] == "gemini"
    assert contracts[0]["request_phase"] == "provisional"
    assert reconstruct(contracts, contracts[0]["translation_request_id"])["differences"] == []
    return request, store, subtitles


def test_gemini_provisional_promotes_without_final_http(provider_config):
    from modules.activity_context import bind_activity_snapshot
    from modules.profile_context import bind_profile_snapshot
    translator = tr.Translator()
    request, store, subtitles = _preview(translator)
    candidate = store.close(request.provisional_id)
    assert candidate.engine == "gemini"
    assert candidate.model == "gemini-3.8-flash"
    assert subtitles.get_nowait().phase == "provisional"
    with bind_activity_snapshot(request.activity_snapshot), bind_profile_snapshot(request.profile_snapshot), patch("urllib.request.urlopen") as http:
        outcome = translator.translate_event(request.text, True, provisional_candidate=candidate,
            source_utterance_ids=request.source_utterance_ids, evidence_source_utterance_ids=request.evidence_source_utterance_ids)
    assert outcome.result_source == "provisional_promotion"
    assert outcome.engine == "gemini"
    assert outcome.target_text == "今天直播很有趣"
    http.assert_not_called()


@pytest.mark.parametrize("field,value", [("engine", "deepseek"), ("model", "old-gemini-model")])
def test_promotion_rejects_old_provider_even_with_matching_fingerprint(provider_config, field, value):
    from modules.activity_context import bind_activity_snapshot
    from modules.profile_context import bind_profile_snapshot
    translator = tr.Translator()
    request, store, _ = _preview(translator)
    candidate = replace(store.close(request.provisional_id), **{field: value})
    with bind_activity_snapshot(request.activity_snapshot), bind_profile_snapshot(request.profile_snapshot), patch("urllib.request.urlopen", return_value=Response(answer(text="今天直播很好看"))) as http:
        outcome = translator.translate_event(request.text, True, provisional_candidate=candidate,
            source_utterance_ids=request.source_utterance_ids, evidence_source_utterance_ids=request.evidence_source_utterance_ids)
    assert outcome.result_source != "provisional_promotion"
    assert outcome.engine == "gemini"
    assert http.call_count == 1


def test_provider_switch_between_guard_and_publication_retires_preview(provider_config):
    translator = tr.Translator()
    request, store, subtitles = _preview(translator, switch_at_guard=True)
    assert store.is_closed(request.provisional_id)
    assert store.candidate(request.provisional_id) is None
    assert subtitles.empty()


@pytest.mark.parametrize("target", ["こんにちは", "안녕하세요"])
def test_gemini_raw_boundary_survives_general_correction(provider_config, target):
    def correct(source, text, *, name_render_only=False):
        return (text, []) if name_render_only else ("你好", [{"stage": "general"}])
    with patch.object(tr, "_preview_source_aware_corrections", side_effect=correct):
        guard = tr._translation_output_guard(engines.GeminiTranslationEngine(), target, "hello")
    assert guard["reason"] in {"unexpected_japanese", "unexpected_hangul"}
    assert guard["candidate_output"] == "你好"
    assert any(row["reason"].startswith("raw_unexpected_") for row in guard["failed_invariants"])


def test_gemini_exact_name_render_rescue_and_evidence(provider_config):
    from tests.test_translator import _active_translation_profile
    with _active_translation_profile("isegye_lilpa"):
        guard = tr._translation_output_guard(engines.GeminiTranslationEngine(), "릴파", "릴파")
    assert not guard.get("reason")
    assert guard["accepted_after_name_render"] == ["unexpected_hangul"]
    assert guard["candidate_output"] == "Lilpa"


def test_deepseek_route_fingerprint_and_cache_identity_unaffected_by_provider_setting(provider_config):
    from modules.activity_context import capture_activity_snapshot, bind_activity_snapshot
    from modules.profile_context import profile_state, bind_profile_snapshot
    provider_config("deepseek")
    translator = tr.Translator()
    engine = engines.DeepSeekTranslationEngine()
    snapshot = capture_activity_snapshot("chatting", source="manual")
    def identities():
        frozen = route(engine)
        fingerprint = provisional.provisional_fingerprint(
            prepared_source="hello", source_utterance_ids=("one",), evidence_source_utterance_ids=("one",),
            profile_id="", activity_cache_identity="fixed", history_cohort=("fixed", "", 0),
            messages=frozen.messages, incomplete=False, provider_options=frozen.options_dict(),
            timeout_seconds=frozen.timeout_seconds)
        cache_identity = translator._prompt_version_for_engine(
            engine, "translate", route_request=frozen, history=[])
        return frozen, fingerprint, cache_identity
    with bind_activity_snapshot(snapshot), bind_profile_snapshot(profile_state.current()):
        old, old_fingerprint, old_cache = identities()
        provider_config("gemini")
        current, current_fingerprint, current_cache = identities()
        gemini = engines.GeminiTranslationEngine()
        gemini_cache = translator._prompt_version_for_engine(
            gemini, "translate", route_request=route(gemini), history=[])
    assert old.body == current.body
    assert old.identity_payload() == current.identity_payload()
    assert old_fingerprint == current_fingerprint
    assert old_cache == current_cache
    assert gemini_cache != old_cache


@pytest.mark.parametrize("gemini_key,deepseek_key,allowed", [
    ("mock-only", "", True), ("", "mock-only", True), ("", "", False),
])
def test_startup_validates_effective_first_engine(provider_config, monkeypatch, caplog, gemini_key, deepseek_key, allowed):
    import main
    selected = provider_config(key=gemini_key)
    selected = replace(selected, keys=replace(selected.keys, deepseek=deepseek_key))
    monkeypatch.setattr(engines, "cfg", selected)
    monkeypatch.setattr(main, "cfg", selected)
    monkeypatch.setattr(main, "_validate_scene_vision_config", lambda: None)
    with caplog.at_level(logging.WARNING):
        if allowed:
            main._validate_config(False)
        else:
            with pytest.raises(SystemExit):
                main._validate_config(False)
    if allowed and not gemini_key:
        assert "GEMINI_API_KEY missing" in caplog.text


@pytest.mark.parametrize("status,expected", [(402, "billing_error"), (400, "http_4xx"), (429, "rate_limit")])
def test_http_status_message_class(status, expected):
    error = HTTPError("https://example.invalid", status, "x", {}, io.BytesIO(b""))
    assert engines._classify_api_error(error) == ("api_error", expected)
