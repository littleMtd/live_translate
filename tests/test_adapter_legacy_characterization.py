"""Characterization of legacy (no route_request) adapter calls.

The fixture was captured from the code before the Phase 2a refactor removed
the adapters' duplicated legacy payload construction. Request bodies, the
urlopen timeout, return values, token usage and every non-timing diagnostic
must stay identical for legacy callers.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

import modules.translation_engines as te
from modules.translation_engines import (
    DeepSeekTranslationEngine,
    GroqTranslationEngine,
)

FIXTURE = Path(__file__).parent / "fixtures" / "phase2_legacy_adapters.json"
# Wall-clock measurements and process-wide in-flight counters vary per run.
_VOLATILE = frozenset({
    "api_total_wall_ms", "api_final_attempt_ms", "api_first_attempt_ms",
    "api_retry_attempt_ms", "retry_sleep_ms", "api_inflight_count_at_start",
})
SYSTEM = "Translate to Traditional Chinese."
HISTORY7 = [(f"이전 문장 {i}", f"上一句 {i}") for i in range(7)]
LONG_HISTORY = [("가" * 200 + str(i), "甲" * 260 + str(i)) for i in range(3)]


class _Response:
    def __init__(self, content: str):
        self.data = json.dumps(
            {
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.data


def _http_error(code: int, body: bytes):
    return urllib.error.HTTPError("https://example.invalid", code, "error", {}, io.BytesIO(body))


def _run(call, script):
    """Run one legacy call against a scripted urlopen and capture evidence."""
    bodies: list[str] = []
    timeouts: list = []
    queue = list(script)

    def urlopen(request, timeout=None):
        bodies.append(request.data.decode("utf-8"))
        timeouts.append(timeout)
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        return _Response(step)

    te.reset_last_engine_diagnostics()
    te.reset_last_token_usage()
    with patch("urllib.request.urlopen", side_effect=urlopen), patch(
        "modules.translation_engines.time.sleep", return_value=None
    ):
        result = call()
    diagnostics = {
        key: value
        for key, value in te.get_last_engine_api_diagnostics().items()
        if key not in _VOLATILE
    }
    return {
        "result": result,
        "bodies": bodies,
        "timeouts": timeouts,
        "diagnostics": diagnostics,
        "usage": te.get_last_token_usage(),
    }


def _with_timeout(engine, value):
    engine._timeout = value
    return engine


def _groq():
    engine = GroqTranslationEngine()
    engine._api_key = "mock-only"
    return engine


def _deepseek():
    engine = DeepSeekTranslationEngine()
    engine._api_key = "mock-only"
    return engine


def _cases():
    token_limit = _http_error(413, b'{"error":{"message":"Request too large: token limit"}}')
    return {
        "groq_truncated_history": lambda: _run(
            lambda: _groq().translate("이거 뭐야", SYSTEM, False, LONG_HISTORY), ["譯文"]),
        "groq_token_limit_retry": lambda: _run(
            lambda: _groq().translate("이거 뭐야", SYSTEM, False, HISTORY7), [token_limit, "譯文"]),
        "groq_no_history": lambda: _run(
            lambda: _groq().translate("지금 게임 하고", SYSTEM, True, []), ["譯文"]),
        "groq_timeout_zero": lambda: _run(
            lambda: _with_timeout(_groq(), 0).translate("이거 뭐야", SYSTEM, False, []), ["譯文"]),
        "groq_timeout_none": lambda: _run(
            lambda: _with_timeout(_groq(), None).translate("이거 뭐야", SYSTEM, False, []), ["譯文"]),
        "deepseek_translate_history": lambda: _run(
            lambda: _deepseek().translate("이거 뭐야", SYSTEM, False, HISTORY7), ["譯文"]),
        "deepseek_messages_arbitrary": lambda: _run(
            lambda: _deepseek().translate_messages(
                (("system", "custom system"), ("user", "custom <one>"),
                 ("assistant", "回覆"), ("user", "다음 & 문장"))),
            ["譯文"]),
        "deepseek_messages_empty": lambda: _run(
            lambda: _deepseek().translate_messages(()), ["譯文"]),
    }


def capture_all() -> dict:
    return {name: run() for name, run in _cases().items()}


@pytest.mark.parametrize("case", sorted(_cases()))
def test_legacy_adapter_call_matches_pre_refactor_characterization(case):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(_cases()[case](), ensure_ascii=False))
    assert actual == expected[case]


def test_fixture_covers_every_case():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(expected) == set(_cases())
