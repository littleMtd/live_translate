"""Immutable, provider-ready translation request snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Sequence

from config import cfg
from modules.forensics_contract import (
    REQUEST_CONTRACT_SCHEMA_VERSION,
    history_manifest,
    message_manifest,
    stable_identity,
)
from modules.translation_engines import (
    TranslationEngine,
    DeepSeekTranslationEngine,
    GroqTranslationEngine,
    _groq_model_options,
    build_effective_deepseek_messages,
    build_effective_groq_messages,
    translation_route_id,
)


@dataclass(frozen=True)
class RouteRequest:
    route_id: str
    engine: str
    model: str
    messages: tuple[tuple[str, str], ...]
    retry_messages: tuple[tuple[str, str], ...]
    body: bytes
    retry_body: bytes | None
    provider_options: tuple[tuple[str, str], ...]
    timeout_seconds: float | None

    def options_dict(self) -> dict[str, Any]:
        return {key: json.loads(value) for key, value in self.provider_options}

    def identity_payload(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "messages": list(message_manifest(self.messages)),
            "retry_messages": list(message_manifest(self.retry_messages)),
            "provider_options": self.options_dict(),
            "timeout_seconds": self.timeout_seconds,
            "body_sha256": hashlib.sha256(self.body).hexdigest() if self.body else "",
            "retry_body_sha256": (
                hashlib.sha256(self.retry_body).hexdigest()
                if self.retry_body is not None else ""
            ),
        }


@dataclass(frozen=True)
class TranslationRequest:
    original_source: str
    provider_source: str
    incomplete: bool
    history: tuple[tuple[str, str], ...]
    history_cohort: tuple[str, str, int]
    profile_cache_identity: str
    activity_cache_identity: str
    request_protection_identity: str
    canonical_obligations: tuple[str, ...]
    routes: tuple[RouteRequest, ...]
    history_context_enabled: bool = False
    artifact_hashes: tuple[tuple[str, str], ...] = ()
    phase: str = "final"
    provisional_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": REQUEST_CONTRACT_SCHEMA_VERSION,
            "phase": self.phase,
            "provisional_id": self.provisional_id,
            "original_source": self.original_source,
            "provider_source": self.provider_source,
            "incomplete": self.incomplete,
            "history": list(history_manifest(self.history)),
            "history_context_enabled": self.history_context_enabled,
            "history_cohort": list(self.history_cohort),
            "profile_cache_identity": self.profile_cache_identity,
            "activity_cache_identity": self.activity_cache_identity,
            "request_protection_identity": self.request_protection_identity,
            "canonical_obligations": [json.loads(x) for x in self.canonical_obligations],
            "routes": [route.identity_payload() for route in self.routes],
            "artifact_hashes": dict(self.artifact_hashes),
        }

    @property
    def request_id(self) -> str:
        return stable_identity(self.identity_payload())

    def route_contract_id(self, route: RouteRequest) -> str:
        return stable_identity({"request_id": self.request_id, **route.identity_payload()})

    def route(self, route_id: str) -> RouteRequest | None:
        return next((route for route in self.routes if route.route_id == route_id), None)


def _options(body: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for key, value in body.items() if key != "messages"
    )


def _body(messages: tuple[tuple[str, str], ...], options: dict[str, Any], *,
          compact: bool = False) -> bytes:
    body = {"model": options["model"],
            "messages": [{"role": role, "content": content} for role, content in messages]}
    body.update({key: value for key, value in options.items() if key != "model"})
    if compact:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return json.dumps(body).encode()


def _deepseek_options(engine: DeepSeekTranslationEngine) -> dict[str, Any]:
    return {
        "model": engine.model_name,
        "temperature": cfg.translation.deepseek_temperature,
        "max_tokens": engine._max_tokens,
        "stream": False,
        "thinking": {"type": "disabled"},
    }


def deepseek_route_for_messages(
    engine: DeepSeekTranslationEngine,
    messages: Sequence[tuple[str, str]],
) -> RouteRequest:
    """Freeze caller-supplied DeepSeek messages with the adapter's options."""
    frozen = tuple((role, content) for role, content in messages)
    options = _deepseek_options(engine)
    return RouteRequest(
        route_id=translation_route_id(engine), engine="deepseek",
        model=engine.model_name, messages=frozen, retry_messages=(),
        body=_body(frozen, options, compact=True), retry_body=None,
        provider_options=_options(options),
        timeout_seconds=engine.request_timeout_seconds,
    )


def freeze_route_request(
    engine: TranslationEngine,
    provider_source: str,
    system_prompt: str,
    incomplete: bool,
    history: Sequence[tuple[str, str]] | None,
) -> RouteRequest:
    """Freeze the current adapter's exact primary and retry wire payloads."""
    name = engine.engine_name.lower()
    model = engine.model_name
    selected = list(history or ())
    retry_messages: tuple[tuple[str, str], ...] = ()
    retry_body: bytes | None = None
    if not isinstance(engine, (DeepSeekTranslationEngine, GroqTranslationEngine)):
        messages = (
            build_effective_deepseek_messages(
                provider_source, system_prompt, incomplete, selected
            ) if name == "deepseek" else
            build_effective_groq_messages(
                provider_source, system_prompt, incomplete, selected
            ) if name == "groq" else
            (("system", system_prompt), ("user", provider_source))
        )
        return RouteRequest(
            route_id=translation_route_id(engine), engine=name, model=model,
            messages=messages, retry_messages=(), body=b"", retry_body=None,
            provider_options=(), timeout_seconds=None,
        )
    if name == "deepseek":
        messages = build_effective_deepseek_messages(
            provider_source, system_prompt, incomplete, selected
        )
        options = _deepseek_options(engine)
        body = _body(messages, options, compact=True)
    elif name == "groq":
        messages = build_effective_groq_messages(
            provider_source, system_prompt, incomplete, selected
        )
        options = {
            "model": model,
            "temperature": cfg.translation.temperature,
            "max_tokens": engine._max_tokens,
            **_groq_model_options(model),
        }
        body = _body(messages, options)
        if len(messages) > 2:
            retry_messages = (messages[0], messages[-1])
            retry_options = {**options, "max_tokens": engine._retry_max_tokens}
            retry_body = _body(retry_messages, retry_options)
        options["retry_max_tokens"] = engine._retry_max_tokens
    else:
        raise ValueError(f"unsupported translation route: {name}")
    return RouteRequest(
        route_id=translation_route_id(engine), engine=name, model=model,
        messages=tuple(messages), retry_messages=retry_messages,
        body=body, retry_body=retry_body, provider_options=_options(options),
        timeout_seconds=engine.request_timeout_seconds,
    )
