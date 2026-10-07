from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import _Scene
from modules.scene_vision import (
    VISION_PROVIDER_REGISTRY,
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
            ("groq", "fallback-model"),
        ),
    )

    routes = configured_vision_routes(scene)

    assert [(route.provider, route.model) for route in routes] == [
        ("groq", "groq-model"),
        ("groq", "fallback-model"),
    ]


def test_route_credentials_are_not_selected_opportunistically():
    scene = _Scene(
        vision_provider="groq",
        vision_model="groq-model",
        vision_fallback_routes=(("groq", "fallback-model"),),
    )
    keys = SimpleNamespace(
        groq="",
        groq_fallback="",
        unrelated="unrelated-key",
    )

    assert missing_vision_route_credentials(scene, keys) == (
        "groq:groq-model",
        "groq:fallback-model",
    )


def test_builder_freezes_explicit_provider_model_pairs():
    scene = _Scene(
        vision_provider="groq",
        vision_model="groq-model",
        vision_fallback_routes=(("groq", "fallback-model"),),
    )
    keys = SimpleNamespace(
        groq="groq-key",
        groq_fallback="",
    )

    provider = build_vision_provider(
        "bounded prompt",
        scene_config=scene,
        keys=keys,
    )

    assert provider.route_identities == (
        "groq:groq-model",
        "groq:fallback-model",
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
        "groq",
        "fallback-model",
        success(
            "groq",
            "fallback-model",
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
    assert result.diagnostics.provider == "groq"
    assert result.diagnostics.model == "fallback-model"
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
        "groq",
        "fallback-model",
        success("groq", "fallback-model", "Minecraft"),
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
        "groq",
        "fallback-model",
        success("groq", "fallback-model", "Minecraft"),
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
        "groq",
        "fallback-model",
        success("groq", "fallback-model", "Hades"),
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
        "groq",
        "fallback-model",
        success("groq", "fallback-model", "Minecraft"),
    )

    result = RoutedVisionProvider((noncanonical, paid)).classify(b"jpeg")

    assert result.text == "watching a spreadsheet"
    assert paid.calls == 0
