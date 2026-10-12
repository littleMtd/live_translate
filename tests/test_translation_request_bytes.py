"""Byte-level regression for the Phase 1 provider request refactor."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import urllib.error

import pytest

from config import cfg
from modules.translation_engines import (
    DeepSeekTranslationEngine,
    GroqTranslationEngine,
)
from modules.translation_request import freeze_route_request


_GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "phase1_request_bytes.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(autouse=True)
def _historical_temperature_for_byte_baseline():
    # This fixture records the Phase 1 pre-refactor wire bytes at 1.3.
    historical_cfg = replace(
        cfg, translation=replace(cfg.translation, deepseek_temperature=1.3)
    )
    with patch("modules.translation_engines.cfg", historical_cfg), patch(
        "modules.translation_request.cfg", historical_cfg
    ):
        yield


class _Response:
    def __init__(self, content: str = "譯文") -> None:
        self.data = json.dumps(
            {
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "system_fingerprint": "mock-fingerprint",
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.data


def _capture(engine, *, incomplete: bool, history, retry: str = "") -> list[str]:
    requests: list[str] = []
    engine._api_key = "mock-only"

    def urlopen(request, timeout=None):
        requests.append(request.data.decode("utf-8"))
        if retry == "groq_token_limit" and len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 413, "token limit", {}, io.BytesIO(b"token limit")
            )
        return _Response()

    with patch("urllib.request.urlopen", side_effect=urlopen), patch(
        "modules.translation_engines.time.sleep", return_value=None
    ):
        engine.translate(
            "안녕 <escrow_1> & 세계", "Translate to Traditional Chinese.",
            incomplete, history,
        )
    return requests


def test_provider_request_bytes_match_pre_refactor_baseline():
    history = [("지난 <context_source> & 안녕", "上一句 & 甲")]
    actual = {}
    for name, cls in (
        ("deepseek", DeepSeekTranslationEngine),
        ("groq", GroqTranslationEngine),
    ):
        actual[f"{name}_history"] = _capture(cls(), incomplete=False, history=history)
        actual[f"{name}_incomplete"] = _capture(cls(), incomplete=True, history=[])
    actual["groq_token_limit_retry"] = _capture(
        GroqTranslationEngine(), incomplete=False, history=history,
        retry="groq_token_limit",
    )
    assert actual == _GOLDEN


def test_frozen_route_bodies_match_pre_refactor_baseline():
    history = [("지난 <context_source> & 안녕", "上一句 & 甲")]
    for name, cls in (
        ("deepseek", DeepSeekTranslationEngine),
        ("groq", GroqTranslationEngine),
    ):
        for label, incomplete, selected_history in (
            ("history", False, history), ("incomplete", True, []),
        ):
            route = freeze_route_request(
                cls(), "안녕 <escrow_1> & 세계",
                "Translate to Traditional Chinese.", incomplete, selected_history,
            )
            assert route.body.decode("utf-8") == _GOLDEN[f"{name}_{label}"][0]
            if name == "groq" and label == "history":
                assert route.retry_body.decode("utf-8") == _GOLDEN[
                    "groq_token_limit_retry"
                ][1]
