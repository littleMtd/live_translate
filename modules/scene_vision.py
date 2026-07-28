"""Provider-neutral, explicitly routed scene-vision classification.

Routes are frozen when the resolver starts. Each adapter performs exactly one
SDK/HTTP attempt, and the route runner advances only after a bounded retryable
provider failure. A valid ``unknown`` response is a successful abstention and
never activates a paid fallback.
"""

from __future__ import annotations

import base64
import json
import math
import socket
import time
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import cfg


_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_USER_AGENT = "live_translate/scene-vision"
_MAX_DIAGNOSTIC_INT = 10_000_000
_MAX_RATE_RESET_SEC = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class VisionRoute:
    provider: str
    model: str

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class VisionAttemptDiagnostics:
    provider: str
    model: str
    outcome: str
    retryable: bool
    latency_ms: float | None = None
    error_type: str = ""
    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    api_cost_usd: float | None = None
    rate_limit_tpm: int | None = None
    rate_limit_remaining_tokens: int | None = None
    rate_limit_reset_tokens_sec: float | None = None

    def event_fields(self) -> dict[str, str | int | float | bool]:
        fields: dict[str, str | int | float | bool] = {
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "retryable": self.retryable,
        }
        optional = {
            "latency_ms": self.latency_ms,
            "error_type": self.error_type or None,
            "http_status": self.http_status,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "api_cost_usd": self.api_cost_usd,
            "rate_limit_tpm": self.rate_limit_tpm,
            "rate_limit_remaining_tokens": self.rate_limit_remaining_tokens,
            "rate_limit_reset_tokens_sec": self.rate_limit_reset_tokens_sec,
        }
        fields.update(
            (key, value) for key, value in optional.items() if value is not None
        )
        return fields


@dataclass(frozen=True)
class VisionDiagnostics:
    outcome: str
    attempt_limit: int
    error_type: str = ""
    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    rate_limit_tpm: int | None = None
    rate_limit_remaining_tokens: int | None = None
    rate_limit_reset_tokens_sec: float | None = None
    provider: str = ""
    model: str = ""
    retryable: bool = False
    api_cost_usd: float | None = None
    attempt_chain: tuple[VisionAttemptDiagnostics, ...] = ()

    def event_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "vision_outcome": self.outcome,
            "vision_attempt_limit": self.attempt_limit,
        }
        optional = {
            "vision_error_type": self.error_type or None,
            "vision_http_status": self.http_status,
            "vision_prompt_tokens": self.prompt_tokens,
            "vision_completion_tokens": self.completion_tokens,
            "vision_total_tokens": self.total_tokens,
            "vision_rate_limit_tpm": self.rate_limit_tpm,
            "vision_rate_limit_remaining_tokens": (
                self.rate_limit_remaining_tokens
            ),
            "vision_rate_limit_reset_tokens_sec": (
                self.rate_limit_reset_tokens_sec
            ),
            "vision_provider": self.provider or None,
            "vision_model": self.model or None,
            "vision_api_cost_usd": self.api_cost_usd,
        }
        fields.update(
            (key, value) for key, value in optional.items() if value is not None
        )
        if self.attempt_chain:
            fields.update(
                {
                    "vision_retryable": self.retryable,
                    "vision_attempt_count": len(self.attempt_chain),
                    "vision_route_count": self.attempt_limit,
                    "vision_fallback_used": len(self.attempt_chain) > 1,
                    "vision_attempt_chain": [
                        attempt.event_fields()
                        for attempt in self.attempt_chain
                    ],
                }
            )
        return fields


@dataclass(frozen=True)
class VisionClassification:
    text: str
    diagnostics: VisionDiagnostics


class VisionProviderFailure(RuntimeError):
    def __init__(self, diagnostics: VisionDiagnostics):
        self.diagnostics = diagnostics
        super().__init__(diagnostics.error_type or "provider_error")


class VisionProvider(Protocol):
    provider_name: str
    model_name: str

    def classify(self, jpeg: bytes) -> str | VisionClassification:
        ...


def _nonnegative_int(
    value: object,
    *,
    maximum: int = _MAX_DIAGNOSTIC_INT,
) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= maximum else None


def _nonnegative_float(
    value: object,
    *,
    maximum: float,
) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not 0.0 <= parsed <= maximum:
        return None
    return parsed


def _elapsed_ms(started_at: float) -> float:
    return round(max(0.0, (time.monotonic() - started_at) * 1000), 2)


def _rate_reset_seconds(value: object) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    total = 0.0
    number = ""
    seen_unit = False
    multipliers = {"h": 3600.0, "m": 60.0, "s": 1.0}
    for char in text:
        if char.isdigit() or char == ".":
            number += char
            continue
        if char not in multipliers or not number:
            return None
        try:
            total += float(number) * multipliers[char]
        except ValueError:
            return None
        number = ""
        seen_unit = True
    if number or not seen_unit:
        return None
    if not math.isfinite(total) or total > _MAX_RATE_RESET_SEC:
        return None
    return round(total, 3)


def _attempt(
    route: VisionRoute,
    *,
    outcome: str,
    retryable: bool,
    started_at: float,
    error_type: str = "",
    http_status: object = None,
    prompt_tokens: object = None,
    completion_tokens: object = None,
    total_tokens: object = None,
    api_cost_usd: object = None,
    rate_limit_tpm: object = None,
    rate_limit_remaining_tokens: object = None,
    rate_limit_reset_tokens_sec: object = None,
) -> VisionAttemptDiagnostics:
    return VisionAttemptDiagnostics(
        provider=route.provider,
        model=route.model,
        outcome=outcome,
        retryable=retryable,
        latency_ms=_elapsed_ms(started_at),
        error_type=error_type,
        http_status=_nonnegative_int(http_status, maximum=599),
        prompt_tokens=_nonnegative_int(prompt_tokens),
        completion_tokens=_nonnegative_int(completion_tokens),
        total_tokens=_nonnegative_int(total_tokens),
        api_cost_usd=_nonnegative_float(api_cost_usd, maximum=1000.0),
        rate_limit_tpm=_nonnegative_int(rate_limit_tpm),
        rate_limit_remaining_tokens=_nonnegative_int(
            rate_limit_remaining_tokens
        ),
        rate_limit_reset_tokens_sec=_nonnegative_float(
            rate_limit_reset_tokens_sec,
            maximum=float(_MAX_RATE_RESET_SEC),
        ),
    )


def _diagnostics_for_attempt(
    attempt: VisionAttemptDiagnostics,
    *,
    attempt_limit: int = 1,
) -> VisionDiagnostics:
    return VisionDiagnostics(
        outcome=attempt.outcome,
        attempt_limit=attempt_limit,
        error_type=attempt.error_type,
        http_status=attempt.http_status,
        prompt_tokens=attempt.prompt_tokens,
        completion_tokens=attempt.completion_tokens,
        total_tokens=attempt.total_tokens,
        rate_limit_tpm=attempt.rate_limit_tpm,
        rate_limit_remaining_tokens=attempt.rate_limit_remaining_tokens,
        rate_limit_reset_tokens_sec=attempt.rate_limit_reset_tokens_sec,
        provider=attempt.provider,
        model=attempt.model,
        retryable=attempt.retryable,
        api_cost_usd=attempt.api_cost_usd,
        attempt_chain=(attempt,),
    )


def _merged_diagnostics(
    attempts: list[VisionAttemptDiagnostics],
    *,
    attempt_limit: int,
) -> VisionDiagnostics:
    selected = attempts[-1]
    return VisionDiagnostics(
        outcome=selected.outcome,
        attempt_limit=attempt_limit,
        error_type=selected.error_type,
        http_status=selected.http_status,
        prompt_tokens=selected.prompt_tokens,
        completion_tokens=selected.completion_tokens,
        total_tokens=selected.total_tokens,
        rate_limit_tpm=selected.rate_limit_tpm,
        rate_limit_remaining_tokens=selected.rate_limit_remaining_tokens,
        rate_limit_reset_tokens_sec=selected.rate_limit_reset_tokens_sec,
        provider=selected.provider,
        model=selected.model,
        retryable=selected.retryable,
        api_cost_usd=selected.api_cost_usd,
        attempt_chain=tuple(attempts),
    )


class GroqVisionProvider:
    provider_name = "groq"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        prompt: str = "",
        api_key: str | None = None,
        timeout: float | None = None,
    ):
        self.model_name = str(
            model_name
            if model_name is not None
            else getattr(cfg.scene, "vision_model", "")
        )
        self._prompt = str(prompt)
        self._api_key = (
            str(api_key)
            if api_key is not None
            else str(cfg.keys.groq or cfg.keys.groq_fallback)
        )
        self._timeout = float(
            timeout
            if timeout is not None
            else getattr(cfg.scene, "vision_timeout", 20.0)
        )

    def classify(self, jpeg: bytes) -> VisionClassification:
        route = VisionRoute(self.provider_name, self.model_name)
        if not self._api_key or not self.model_name or not self._prompt:
            started_at = time.monotonic()
            attempt = _attempt(
                route,
                outcome="error",
                retryable=False,
                started_at=started_at,
                error_type="configuration_error",
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt))

        from groq import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            Groq,
            RateLimitError,
        )

        client = Groq(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=0,
        )
        started_at = time.monotonic()
        try:
            raw_response = client.chat.completions.with_raw_response.create(
                model=self.model_name,
                max_tokens=20,
                temperature=0,
                reasoning_effort="none",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self._prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/jpeg;base64,"
                                    + base64.b64encode(jpeg).decode("ascii"),
                                },
                            },
                        ],
                    }
                ],
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(exc, RateLimitError):
                error_type, retryable = "rate_limit", True
            elif isinstance(exc, APITimeoutError):
                error_type, retryable = "timeout", True
            elif isinstance(exc, APIConnectionError):
                error_type, retryable = "connection_error", True
            elif isinstance(exc, APIStatusError):
                parsed_status = _nonnegative_int(status, maximum=599)
                if parsed_status == 408:
                    error_type, retryable = "timeout", True
                elif parsed_status in {401, 403}:
                    error_type, retryable = "auth_error", False
                elif parsed_status == 402:
                    error_type, retryable = "payment_required", False
                else:
                    error_type = "http_error"
                    retryable = bool(parsed_status and parsed_status >= 500)
            else:
                error_type, retryable = "provider_error", False
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            get_header = getattr(headers, "get", None)
            header = (
                (lambda name: get_header(name))
                if callable(get_header)
                else (lambda _name: None)
            )
            attempt = _attempt(
                route,
                outcome="error",
                retryable=retryable,
                started_at=started_at,
                error_type=error_type,
                http_status=status,
                rate_limit_tpm=header("x-ratelimit-limit-tokens"),
                rate_limit_remaining_tokens=header(
                    "x-ratelimit-remaining-tokens"
                ),
                rate_limit_reset_tokens_sec=_rate_reset_seconds(
                    header("x-ratelimit-reset-tokens")
                ),
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None

        headers = getattr(raw_response, "headers", None)
        get_header = getattr(headers, "get", None)
        header = (
            (lambda name: get_header(name))
            if callable(get_header)
            else (lambda _name: None)
        )
        try:
            response = raw_response.parse()
            usage = getattr(response, "usage", None)
            content = str(response.choices[0].message.content or "").strip()
        except Exception:
            attempt = _attempt(
                route,
                outcome="error",
                retryable=True,
                started_at=started_at,
                error_type="parse_error",
                rate_limit_tpm=header("x-ratelimit-limit-tokens"),
                rate_limit_remaining_tokens=header(
                    "x-ratelimit-remaining-tokens"
                ),
                rate_limit_reset_tokens_sec=_rate_reset_seconds(
                    header("x-ratelimit-reset-tokens")
                ),
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None
        if not content:
            attempt = _attempt(
                route,
                outcome="error",
                retryable=True,
                started_at=started_at,
                error_type="empty_response",
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
                rate_limit_tpm=header("x-ratelimit-limit-tokens"),
                rate_limit_remaining_tokens=header(
                    "x-ratelimit-remaining-tokens"
                ),
                rate_limit_reset_tokens_sec=_rate_reset_seconds(
                    header("x-ratelimit-reset-tokens")
                ),
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt))
        attempt = _attempt(
            route,
            outcome="success",
            retryable=False,
            started_at=started_at,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            rate_limit_tpm=header("x-ratelimit-limit-tokens"),
            rate_limit_remaining_tokens=header(
                "x-ratelimit-remaining-tokens"
            ),
            rate_limit_reset_tokens_sec=_rate_reset_seconds(
                header("x-ratelimit-reset-tokens")
            ),
        )
        return VisionClassification(
            text=content,
            diagnostics=_diagnostics_for_attempt(attempt),
        )


class OpenRouterVisionProvider:
    provider_name = "openrouter"

    def __init__(
        self,
        *,
        model_name: str,
        prompt: str,
        api_key: str,
        timeout: float,
        urlopen_fn: Callable[..., Any] = urlopen,
    ):
        self.model_name = str(model_name)
        self._prompt = str(prompt)
        self._api_key = str(api_key)
        self._timeout = float(timeout)
        self._urlopen = urlopen_fn

    def classify(self, jpeg: bytes) -> VisionClassification:
        route = VisionRoute(self.provider_name, self.model_name)
        started_at = time.monotonic()
        if not self._api_key or not self.model_name or not self._prompt:
            attempt = _attempt(
                route,
                outcome="error",
                retryable=False,
                started_at=started_at,
                error_type="configuration_error",
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt))
        body = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(jpeg).decode("ascii"),
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 20,
        }
        request = Request(
            _OPENROUTER_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _OPENROUTER_USER_AGENT,
                "Authorization": f"Bearer {self._api_key}",
                "HTTP-Referer": "http://localhost/live_translate",
                "X-Title": "live_translate",
            },
        )
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise TypeError
            usage_value = payload.get("usage")
            if usage_value is None:
                usage: dict[str, Any] = {}
            elif isinstance(usage_value, dict):
                usage = usage_value
            else:
                raise TypeError
            choices = payload["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            choice = choices[0]
            if not isinstance(choice, dict):
                raise TypeError
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
            content_value = message.get("content")
            if content_value is None:
                content = ""
            elif isinstance(content_value, str):
                content = content_value.strip()
            else:
                raise TypeError
        except HTTPError as exc:
            status = _nonnegative_int(exc.code, maximum=599)
            if status == 429:
                error_type, retryable = "rate_limit", True
            elif status == 408:
                error_type, retryable = "timeout", True
            elif status in {401, 403}:
                error_type, retryable = "auth_error", False
            elif status == 402:
                error_type, retryable = "payment_required", False
            else:
                error_type = "http_error"
                retryable = bool(status and status >= 500)
            attempt = _attempt(
                route,
                outcome="error",
                retryable=retryable,
                started_at=started_at,
                error_type=error_type,
                http_status=status,
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None
        except (TimeoutError, socket.timeout) as exc:
            attempt = _attempt(
                route,
                outcome="error",
                retryable=True,
                started_at=started_at,
                error_type="timeout",
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None
        except URLError as exc:
            is_timeout = isinstance(exc.reason, (TimeoutError, socket.timeout))
            attempt = _attempt(
                route,
                outcome="error",
                retryable=True,
                started_at=started_at,
                error_type="timeout" if is_timeout else "connection_error",
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            attempt = _attempt(
                route,
                outcome="error",
                retryable=True,
                started_at=started_at,
                error_type="parse_error",
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None
        except Exception:
            attempt = _attempt(
                route,
                outcome="error",
                retryable=False,
                started_at=started_at,
                error_type="provider_error",
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt)) from None

        if not content:
            attempt = _attempt(
                route,
                outcome="error",
                retryable=True,
                started_at=started_at,
                error_type="empty_response",
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                api_cost_usd=usage.get("cost"),
            )
            raise VisionProviderFailure(_diagnostics_for_attempt(attempt))
        attempt = _attempt(
            route,
            outcome="success",
            retryable=False,
            started_at=started_at,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            api_cost_usd=usage.get("cost"),
        )
        return VisionClassification(
            text=content,
            diagnostics=_diagnostics_for_attempt(attempt),
        )


class RoutedVisionProvider:
    """Run an immutable ordered provider list with bounded failure fallback."""

    def __init__(self, providers: tuple[VisionProvider, ...]):
        if not providers:
            raise ValueError("at least one scene vision provider is required")
        identities = [
            (provider.provider_name, provider.model_name)
            for provider in providers
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("scene vision routes must be unique")
        self._providers = providers
        self.provider_name = providers[0].provider_name
        self.model_name = providers[0].model_name
        self.route_identities = tuple(
            f"{provider}:{model}" for provider, model in identities
        )

    def classify(self, jpeg: bytes) -> VisionClassification:
        attempts: list[VisionAttemptDiagnostics] = []
        for index, provider in enumerate(self._providers):
            try:
                result = provider.classify(jpeg)
            except VisionProviderFailure as exc:
                if exc.diagnostics.attempt_chain:
                    attempts.extend(exc.diagnostics.attempt_chain)
                else:
                    attempts.append(
                        VisionAttemptDiagnostics(
                            provider=provider.provider_name,
                            model=provider.model_name,
                            outcome="error",
                            retryable=exc.diagnostics.retryable,
                            error_type=exc.diagnostics.error_type,
                            http_status=exc.diagnostics.http_status,
                        )
                    )
                can_fallback = (
                    attempts[-1].retryable
                    and index + 1 < len(self._providers)
                )
                if can_fallback:
                    continue
                raise VisionProviderFailure(
                    _merged_diagnostics(
                        attempts,
                        attempt_limit=len(self._providers),
                    )
                ) from None
            except Exception:
                attempts.append(
                    VisionAttemptDiagnostics(
                        provider=provider.provider_name,
                        model=provider.model_name,
                        outcome="error",
                        retryable=False,
                        error_type="provider_error",
                    )
                )
                raise VisionProviderFailure(
                    _merged_diagnostics(
                        attempts,
                        attempt_limit=len(self._providers),
                    )
                ) from None

            if isinstance(result, VisionClassification):
                if result.diagnostics.attempt_chain:
                    attempts.extend(result.diagnostics.attempt_chain)
                else:
                    attempts.append(
                        VisionAttemptDiagnostics(
                            provider=provider.provider_name,
                            model=provider.model_name,
                            outcome="success",
                            retryable=False,
                        )
                    )
                if not str(result.text or "").strip():
                    attempts[-1] = replace(
                        attempts[-1],
                        outcome="error",
                        retryable=True,
                        error_type="empty_response",
                    )
                    if index + 1 < len(self._providers):
                        continue
                    raise VisionProviderFailure(
                        _merged_diagnostics(
                            attempts,
                            attempt_limit=len(self._providers),
                        )
                    )
                return VisionClassification(
                    text=result.text,
                    diagnostics=_merged_diagnostics(
                        attempts,
                        attempt_limit=len(self._providers),
                    ),
                )
            attempts.append(
                VisionAttemptDiagnostics(
                    provider=provider.provider_name,
                    model=provider.model_name,
                    outcome="success" if str(result or "").strip() else "error",
                    retryable=not bool(str(result or "").strip()),
                    error_type=(
                        "" if str(result or "").strip() else "empty_response"
                    ),
                )
            )
            if not str(result or "").strip():
                if index + 1 < len(self._providers):
                    continue
                raise VisionProviderFailure(
                    _merged_diagnostics(
                        attempts,
                        attempt_limit=len(self._providers),
                    )
                )
            return VisionClassification(
                text=str(result or ""),
                diagnostics=_merged_diagnostics(
                    attempts,
                    attempt_limit=len(self._providers),
                ),
            )
        raise AssertionError("unreachable scene vision route state")


ProviderFactory = Callable[[VisionRoute, str, object, float], VisionProvider]


def _build_groq(
    route: VisionRoute,
    prompt: str,
    keys: object,
    timeout: float,
) -> VisionProvider:
    return GroqVisionProvider(
        model_name=route.model,
        prompt=prompt,
        api_key=str(
            getattr(keys, "groq", "")
            or getattr(keys, "groq_fallback", "")
        ),
        timeout=timeout,
    )


def _build_openrouter(
    route: VisionRoute,
    prompt: str,
    keys: object,
    timeout: float,
) -> VisionProvider:
    return OpenRouterVisionProvider(
        model_name=route.model,
        prompt=prompt,
        api_key=str(getattr(keys, "openrouter", "") or ""),
        timeout=timeout,
    )


VISION_PROVIDER_REGISTRY: MappingProxyType = MappingProxyType(
    {
        "groq": _build_groq,
        "openrouter": _build_openrouter,
    }
)


def configured_vision_routes(scene_config: object | None = None) -> tuple[VisionRoute, ...]:
    scene = scene_config or cfg.scene
    primary = VisionRoute(
        str(getattr(scene, "vision_provider", "") or ""),
        str(getattr(scene, "vision_model", "") or ""),
    )
    fallback = tuple(
        VisionRoute(str(provider or ""), str(model or ""))
        for provider, model in getattr(scene, "vision_fallback_routes", ())
    )
    routes = (primary, *fallback)
    identities = [route.identity for route in routes]
    if (
        any(
            not route.provider
            or not route.model
            or route.provider not in VISION_PROVIDER_REGISTRY
            for route in routes
        )
        or len(set(identities)) != len(identities)
    ):
        raise ValueError("invalid scene vision route configuration")
    return routes


def missing_vision_route_credentials(
    scene_config: object | None = None,
    keys: object | None = None,
) -> tuple[str, ...]:
    key_config = keys or cfg.keys
    missing = []
    for route in configured_vision_routes(scene_config):
        if route.provider == "groq":
            configured = bool(
                getattr(key_config, "groq", "")
                or getattr(key_config, "groq_fallback", "")
            )
        else:
            configured = bool(getattr(key_config, route.provider, ""))
        if not configured:
            missing.append(route.identity)
    return tuple(missing)


def build_vision_provider(
    prompt: str,
    *,
    scene_config: object | None = None,
    keys: object | None = None,
) -> RoutedVisionProvider:
    scene = scene_config or cfg.scene
    key_config = keys or cfg.keys
    routes = configured_vision_routes(scene)
    timeout = float(getattr(scene, "vision_timeout", 20.0))
    providers = tuple(
        VISION_PROVIDER_REGISTRY[route.provider](
            route,
            str(prompt),
            key_config,
            timeout,
        )
        for route in routes
    )
    return RoutedVisionProvider(providers)
