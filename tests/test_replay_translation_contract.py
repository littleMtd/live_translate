"""Offline contract reconstruction and legacy readability."""

from __future__ import annotations

import hashlib
import io
import json
import queue
import sys
import threading
import time
from copy import deepcopy
from unittest.mock import patch

import pytest

from modules.forensics_contract import (
    history_manifest, message_manifest, sha256_text, stable_identity,
)
from modules.translation_engines import DeepSeekTranslationEngine, GroqTranslationEngine
from modules.translation_request import TranslationRequest, freeze_route_request
from scripts import replay_translation_contract
from scripts.replay_translation_contract import reconstruct
from modules import translator as translator_module
from modules.activity_context import capture_activity_snapshot
from modules.provisional_subtitles import ProvisionalRequest


def _events():
    source = "안녕 <escrow_1>"
    history = (("지난 말", "上一句"),)
    routes = tuple(
        freeze_route_request(engine, source, "Translate to Traditional Chinese.",
                             False, history)
        for engine in (DeepSeekTranslationEngine(), GroqTranslationEngine())
    )
    request = TranslationRequest(
        original_source="안녕", provider_source=source, incomplete=False,
        history=history, history_cohort=("session", "chatting", 1),
        profile_cache_identity="profile-1", activity_cache_identity="activity-1",
        request_protection_identity="protected-1", canonical_obligations=(),
        routes=routes,
    )
    rows = []
    for index, route in enumerate(routes):
        rows.append({
            "event_type": "translation_request_contract",
            "request_contract_schema_version": 2,
            "translation_request_id": request.request_id,
            "request_contract_id": request.route_contract_id(route),
            "request_phase": "final",
            "route_index": index,
            "route_count": len(routes),
            "route_body_json_style": "compact_utf8" if route.engine == "deepseek" else "python_default",
            "route_id": route.route_id,
            "engine": route.engine,
            "model": route.model,
            "original_source": request.original_source,
            "provider_source": request.provider_source,
            "provider_source_sha256": sha256_text(request.provider_source),
            "incomplete": request.incomplete,
            "history": list(history_manifest(history)),
            "history_cohort": list(request.history_cohort),
            "history_context_enabled": request.history_context_enabled,
            "profile_cache_identity": request.profile_cache_identity,
            "activity_cache_identity": request.activity_cache_identity,
            "request_protection_identity": request.request_protection_identity,
            "canonical_obligations": [],
            "messages": list(message_manifest(route.messages)),
            "messages_sha256": stable_identity({"messages": list(route.messages)}),
            "effective_system_prompt": route.messages[0][1],
            "effective_system_prompt_sha256": sha256_text(route.messages[0][1]),
            "retry_messages": list(message_manifest(route.retry_messages)),
            "provider_options": route.options_dict(),
            "timeout_seconds": route.timeout_seconds,
            "body_sha256": hashlib.sha256(route.body).hexdigest(),
            "retry_body_sha256": hashlib.sha256(route.retry_body).hexdigest()
            if route.retry_body is not None else "",
        })
    return request.request_id, rows


def test_replay_reconstructs_complete_v2_request_and_detects_body_difference():
    request_id, rows = _events()
    assert reconstruct(rows, request_id)["differences"] == []
    tampered = deepcopy(rows)
    tampered[1]["body_sha256"] = "incorrect"
    assert any(diff["field"] == "body_sha256"
               for diff in reconstruct(tampered, request_id)["differences"])


@pytest.mark.parametrize("field,value", [
    ("provider_source_sha256", "tampered"),
    ("model", "tampered-model"),
    ("effective_system_prompt_sha256", "tampered"),
    ("messages_sha256", "tampered"),
])
def test_replay_reports_component_field_differences(field, value):
    request_id, rows = _events()
    rows[0][field] = value
    assert any(diff["field"] == field
               for diff in reconstruct(rows, request_id)["differences"])


def test_replay_reports_missing_required_component_hash():
    request_id, rows = _events()
    del rows[0]["provider_source_sha256"]
    assert any(diff["field"] == "provider_source_sha256"
               for diff in reconstruct(rows, request_id)["differences"])


def test_replay_rejects_missing_and_duplicate_routes():
    request_id, rows = _events()
    with pytest.raises(ValueError, match="incomplete route set"):
        reconstruct(rows[:1], request_id)
    duplicated = deepcopy(rows)
    duplicated[1]["route_index"] = 0
    with pytest.raises(ValueError, match="duplicate or missing route index"):
        reconstruct(duplicated, request_id)


def test_replay_keeps_explicit_v1_contract_readable_without_inventing_options():
    messages = (("system", "舊提示"), ("user", "안녕"))
    row = {
        "event_type": "translation_request_contract",
        "request_contract_schema_version": 1,
        "request_contract_id": "v1-contract",
        "messages": list(message_manifest(messages)),
    }
    report = reconstruct([row], "v1-contract")
    assert report["messages"] == [list(item) for item in messages]
    assert report["provider_options"] == "unavailable"
    assert report["body_bytes"] == "unavailable"


def test_main_writes_utf8_when_stdout_starts_as_cp950(tmp_path):
    messages = (("system", "翻譯"), ("user", "안녕하세요"))
    event = {
        "event_type": "translation_request_contract",
        "request_contract_schema_version": 1,
        "request_contract_id": "cp950-contract",
        "messages": list(message_manifest(messages)),
    }
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp950")
    argv = ["replay_translation_contract.py", str(events_path),
            "--request-id", "cp950-contract"]
    with patch.object(sys, "argv", argv), patch.object(sys, "stdout", stdout):
        assert replay_translation_contract.main() == 0
        stdout.flush()
    report = json.loads(raw.getvalue().decode("utf-8"))
    assert report["messages"][1] == ["user", "안녕하세요"]


def test_runtime_v2_contract_events_replay_without_provider_io():
    translator = translator_module.Translator()
    deepseek = DeepSeekTranslationEngine()
    groq = GroqTranslationEngine()
    deepseek._api_key = groq._api_key = "mock-only"
    translator._engines = [deepseek, groq]
    source = "안녕하세요 오늘 방송이에요"
    context = translator_module._resolve_entity_request_context(source)
    obligations = translator_module._canonical_obligations_for_request(context)
    protection = translator_module._request_protection_for(
        source, context, obligations
    )
    prompt = translator._build_system_prompt(context.capsule)
    events = []

    def emit_once(event_type, _key, **fields):
        events.append({"event_type": event_type, **fields})

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "你好，今天開播了。"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "prompt_cache_hit_tokens": 0,
                          "prompt_cache_miss_tokens": 1},
                "system_fingerprint": "mock-fingerprint",
            }).encode()

    with patch.object(translator_module.runtime_events, "emit_once", side_effect=emit_once), patch(
        "urllib.request.urlopen", return_value=Response()
    ):
        translator._call_with_fallback(
            protection, prompt, False, [], canonical_obligations=obligations
        )
    assert len(events) == 2
    report = reconstruct(events, events[0]["translation_request_id"])
    assert report["differences"] == []


def test_provisional_v2_contract_replays_and_emits_result_fingerprint():
    sentence_q = queue.Queue()
    provisional_q = queue.Queue()
    subtitle_q = queue.Queue()
    stop = threading.Event()
    finished = threading.Event()
    contracts = []
    completions = []
    engine = DeepSeekTranslationEngine()
    engine._api_key = "mock-only"

    def emit_once(event_type, _key, **fields):
        if event_type == "translation_request_contract":
            contracts.append({"event_type": event_type, **fields})

    def emit(event_type, **fields):
        if event_type == "provisional_translation" and fields.get("action") == "api_attempt_completed":
            completions.append(fields)
            finished.set()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "你好。"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "prompt_cache_hit_tokens": 0,
                          "prompt_cache_miss_tokens": 1},
                "system_fingerprint": "provisional-fingerprint",
            }).encode()

    request = ProvisionalRequest(
        provisional_id="provisional:phase1-replay", text="안녕하세요",
        incomplete=True, profile_id="", source_utterance_ids=("utt-1",),
        evidence_source_utterance_ids=("utt-1",),
        activity_snapshot=capture_activity_snapshot("chatting", source="manual"),
        requested_at_monotonic=time.monotonic(),
        first_stt_ready_at_monotonic=time.monotonic(),
    )
    with patch.object(translator_module, "deepseek_provisional_eligible", return_value=True), patch.object(
        translator_module, "DeepSeekTranslationEngine", return_value=engine
    ), patch.object(translator_module.runtime_events, "emit_once", side_effect=emit_once), patch.object(
        translator_module.runtime_events, "emit", side_effect=emit
    ), patch("urllib.request.urlopen", return_value=Response()):
        thread = translator_module.start(
            sentence_q, subtitle_q, stop, provisional_queue=provisional_q,
        )
        try:
            provisional_q.put(request)
            assert finished.wait(timeout=3)
        finally:
            stop.set()
            thread.join(timeout=3)
    assert len(contracts) == 1
    assert reconstruct(contracts, contracts[0]["translation_request_id"])["differences"] == []
    assert completions[0]["provider_options"] == contracts[0]["provider_options"]
    assert completions[0]["system_fingerprint"] == "provisional-fingerprint"
