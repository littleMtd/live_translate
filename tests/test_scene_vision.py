from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from config import _Scene
from modules.scene_vision import (
    VISION_PROVIDER_REGISTRY,
    OpenRouterVisionProvider,
    RoutedVisionProvider,
    VisionAttemptDiagnostics,
    VisionClassification,
    VisionDiagnostics,
    VisionProviderFailure,
    build_vision_provider,
    configured_vision_routes,
    missing_vision_route_credentials,
)


def diagnostics(
    provider: str,
    model: str,
    *,
    outcome: str,
    retryable: bool,
    error_type: str = "",
    prompt_tokens: int | None = None,
    total_tokens: int | None = None,
    api_cost_usd: float | None = None,
) -> VisionDiagnostics:
    attempt = VisionAttemptDiagnostics(
        provider=provider,
        model=model,
        outcome=outcome,
        retryable=retryable,
        latency_ms=12.5,
        error_type=error_type,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
        api_cost_usd=api_cost_usd,
    )
    return VisionDiagnostics(
        outcome=outcome,
        attempt_limit=1,
        error_type=error_type,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
        provider=provider,
        model=model,
        retryable=retryable,
        api_cost_usd=api_cost_usd,
        attempt_chain=(attempt,),
    )


class FakeProvider:
    def __init__(self, provider: str, model: str, result):
        self.provider_name = provider
        self.model_name = model
        self.result = result
        self.calls = 0

    def classify(self, jpeg: bytes):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result()
        return self.result


def failure(
    provider: str,
    model: str,
    error_type: str,
    *,
    retryable: bool,
) -> VisionProviderFailure:
    return VisionProviderFailure(
        diagnostics(
            provider,
            model,
            outcome="error",
            retryable=retryable,
            error_type=error_type,
        )
    )


def success(
    provider: str,
    model: str,
    text: str,
    *,
    prompt_tokens: int | None = None,
    total_tokens: int | None = None,
    api_cost_usd: float | None = None,
) -> VisionClassification:
    return VisionClassification(
        text=text,
        diagnostics=diagnostics(
            provider,
            model,
            outcome="success",
            retryable=False,
            prompt_tokens=prompt_tokens,
            total_tokens=total_tokens,
            api_cost_usd=api_cost_usd,
        ),
    )


def test_registry_is_immutable_and_config_routes_are_explicit():
    with pytest.raises(TypeError):
        VISION_PROVIDER_REGISTRY["other"] = object()  # type: ignore[index]

    scene = _Scene(
        vision_provider="groq",
        vision_model="groq-model",
        vision_fallback_routes=(
            ("openrouter", "qwen/qwen3-vl-32b-instruct"),
        ),
    )

    routes = configured_vision_routes(scene)

    assert [(route.provider, route.model) for route in routes] == [
        ("groq", "groq-model"),
        ("openrouter", "qwen/qwen3-vl-32b-instruct"),
    ]


def test_route_credentials_are_not_selected_opportunistically():
    scene = _Scene(
        vision_provider="groq",
        vision_model="groq-model",
        vision_fallback_routes=(("openrouter", "openrouter-model"),),
    )
    keys = SimpleNamespace(
        groq="",
        groq_fallback="",
        openrouter="openrouter-key",
    )

    assert missing_vision_route_credentials(scene, keys) == (
        "groq:groq-model",
    )


def test_builder_freezes_explicit_provider_model_pairs():
    scene = _Scene(
        vision_provider="groq",
        vision_model="groq-model",
        vision_fallback_routes=(("openrouter", "openrouter-model"),),
    )
    keys = SimpleNamespace(
        groq="groq-key",
        groq_fallback="",
        openrouter="openrouter-key",
    )

    provider = build_vision_provider(
        "bounded prompt",
        scene_config=scene,
        keys=keys,
    )

    assert provider.route_identities == (
        "groq:groq-model",
        "openrouter:openrouter-model",
    )
    assert provider.provider_name == "groq"
    assert provider.model_name == "groq-model"


def test_retryable_primary_failure_reaches_only_explicit_fallback():
    primary = FakeProvider(
        "groq",
        "groq-model",
        failure("groq", "groq-model", "timeout", retryable=True),
    )
    fallback = FakeProvider(
        "openrouter",
        "openrouter-model",
        success(
            "openrouter",
            "openrouter-model",
            "League of Legends",
            prompt_tokens=900,
            total_tokens=904,
            api_cost_usd=0.00006,
        ),
    )
    provider = RoutedVisionProvider((primary, fallback))

    result = provider.classify(b"jpeg")

    assert result.text == "League of Legends"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.diagnostics.provider == "openrouter"
    assert result.diagnostics.model == "openrouter-model"
    assert result.diagnostics.attempt_limit == 2
    assert [attempt.outcome for attempt in result.diagnostics.attempt_chain] == [
        "error",
        "success",
    ]
    fields = result.diagnostics.event_fields()
    assert fields["vision_fallback_used"] is True
    assert fields["vision_attempt_count"] == 2
    assert fields["vision_api_cost_usd"] == 0.00006


def test_valid_unknown_stops_without_paid_fallback():
    primary = FakeProvider(
        "groq",
        "groq-model",
        success("groq", "groq-model", "unknown"),
    )
    fallback = FakeProvider(
        "openrouter",
        "openrouter-model",
        success("openrouter", "openrouter-model", "Minecraft"),
    )

    result = RoutedVisionProvider((primary, fallback)).classify(b"jpeg")

    assert result.text == "unknown"
    assert primary.calls == 1
    assert fallback.calls == 0
    assert len(result.diagnostics.attempt_chain) == 1


def test_nonretryable_auth_failure_stops_without_fallback():
    primary = FakeProvider(
        "groq",
        "groq-model",
        failure("groq", "groq-model", "auth_error", retryable=False),
    )
    fallback = FakeProvider(
        "openrouter",
        "openrouter-model",
        success("openrouter", "openrouter-model", "Minecraft"),
    )

    with pytest.raises(VisionProviderFailure) as captured:
        RoutedVisionProvider((primary, fallback)).classify(b"jpeg")

    assert captured.value.diagnostics.error_type == "auth_error"
    assert captured.value.diagnostics.retryable is False
    assert primary.calls == 1
    assert fallback.calls == 0


def test_empty_success_is_retryable_but_noncanonical_text_is_not():
    empty = FakeProvider(
        "groq",
        "groq-model",
        success("groq", "groq-model", ""),
    )
    fallback = FakeProvider(
        "openrouter",
        "openrouter-model",
        success("openrouter", "openrouter-model", "Hades"),
    )

    result = RoutedVisionProvider((empty, fallback)).classify(b"jpeg")

    assert result.text == "Hades"
    assert result.diagnostics.attempt_chain[0].error_type == "empty_response"

    noncanonical = FakeProvider(
        "groq",
        "groq-model",
        success("groq", "groq-model", "watching a spreadsheet"),
    )
    paid = FakeProvider(
        "openrouter",
        "openrouter-model",
        success("openrouter", "openrouter-model", "Minecraft"),
    )

    result = RoutedVisionProvider((noncanonical, paid)).classify(b"jpeg")

    assert result.text == "watching a spreadsheet"
    assert paid.calls == 0


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_openrouter_adapter_sends_image_and_records_cost_without_raw_text():
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "choices": [
                    {"message": {"content": "League of Legends"}}
                ],
                "usage": {
                    "prompt_tokens": 800,
                    "completion_tokens": 4,
                    "total_tokens": 804,
                    "cost": 0.0000572,
                },
            }
        )

    provider = OpenRouterVisionProvider(
        model_name="qwen/qwen3-vl-32b-instruct",
        prompt="bounded prompt",
        api_key="SECRET KEY",
        timeout=7.0,
        urlopen_fn=fake_urlopen,
    )

    result = provider.classify(b"jpeg")

    body = json.loads(captured["request"].data)
    assert captured["timeout"] == 7.0
    assert body["model"] == "qwen/qwen3-vl-32b-instruct"
    assert body["messages"][0]["content"][0]["text"] == "bounded prompt"
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert result.text == "League of Legends"
    assert result.diagnostics.api_cost_usd == 0.0000572
    assert result.diagnostics.total_tokens == 804
    assert "SECRET" not in repr(result.diagnostics.event_fields())


def test_openrouter_malformed_usage_is_retryable_and_reaches_fallback():
    def fake_urlopen(_request, *, timeout):
        assert timeout == 7.0
        return FakeResponse(
            {
                "choices": [{"message": {"content": "Minecraft"}}],
                "usage": ["malformed"],
            }
        )

    primary = OpenRouterVisionProvider(
        model_name="primary-model",
        prompt="bounded prompt",
        api_key="test-key",
        timeout=7.0,
        urlopen_fn=fake_urlopen,
    )
    fallback = FakeProvider(
        "groq",
        "fallback-model",
        success("groq", "fallback-model", "League of Legends"),
    )

    result = RoutedVisionProvider((primary, fallback)).classify(b"jpeg")

    assert result.text == "League of Legends"
    assert fallback.calls == 1
    assert result.diagnostics.attempt_chain[0].provider == "openrouter"
    assert result.diagnostics.attempt_chain[0].error_type == "parse_error"
    assert result.diagnostics.attempt_chain[0].retryable is True
    assert result.diagnostics.attempt_chain[1].provider == "groq"


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (401, "auth_error", False),
        (402, "payment_required", False),
        (429, "rate_limit", True),
        (500, "http_error", True),
    ],
)
def test_openrouter_http_failure_boundaries(status, error_type, retryable):
    def fail(*_args, **_kwargs):
        raise HTTPError(
            "https://openrouter.ai/secret",
            status,
            "SECRET PROVIDER MESSAGE",
            {},
            io.BytesIO(b"SECRET BODY"),
        )

    provider = OpenRouterVisionProvider(
        model_name="vision-model",
        prompt="bounded prompt",
        api_key="test-key",
        timeout=7.0,
        urlopen_fn=fail,
    )

    with pytest.raises(VisionProviderFailure) as captured:
        provider.classify(b"jpeg")

    diagnostics_value = captured.value.diagnostics
    assert diagnostics_value.error_type == error_type
    assert diagnostics_value.retryable is retryable
    assert diagnostics_value.http_status == status
    assert "SECRET" not in repr(diagnostics_value.event_fields())
