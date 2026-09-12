"""One request-local ownership contract for deterministic source spans.

Resolved entities, unresolved source-grounded referents, and reviewed semantic
terms all enter this contract before a provider request is built.  Provider
placeholders are resolved once and then reused unchanged by every route.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from modules.semantic_terminology import (
    SemanticTerminologyEscrow,
    resolve_semantic_terminology,
)
from modules.unknown_name_escrow import (
    UnknownNameEscrow,
    UnknownNameEscrowEvaluation,
    resolve_unknown_name_escrow,
)


_PROTECTION_POLICY_VERSION = "request-source-protection-v1"


@dataclass(frozen=True)
class ProtectedSourceSpan:
    """A source span whose rendering owner is deterministic for this request."""

    owner: str
    source_text: str
    source_spans: tuple[tuple[int, int], ...]
    rendering: str
    rule_id: str = ""
    placeholder: str = ""

    def identity_row(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "source_text": self.source_text,
            "source_spans": [list(span) for span in self.source_spans],
            "rendering": self.rendering,
            "rule_id": self.rule_id,
            "placeholder": self.placeholder,
        }


@dataclass(frozen=True)
class ProviderProtectionEvaluation:
    passed: bool
    reason: str
    restored_candidate: str
    semantic_passed: bool
    semantic_reason: str
    unresolved_evaluation: UnknownNameEscrowEvaluation


@dataclass(frozen=True)
class FinalProtectionEvaluation:
    passed: bool
    reason: str


@dataclass(frozen=True)
class RequestProtection:
    """Frozen provider text and restore invariants for one source sentence."""

    original_source: str
    provider_source: str
    spans: tuple[ProtectedSourceSpan, ...]
    unresolved_referents: UnknownNameEscrow
    semantic_terms: SemanticTerminologyEscrow
    identity: str

    @property
    def active(self) -> bool:
        return bool(self.spans)

    @property
    def requires_provider_protection(self) -> bool:
        return self.unresolved_referents.active or self.semantic_terms.active

    @property
    def fingerprint_identity(self) -> str:
        return self.identity if self.active else ""

    @property
    def approved_hangul_terms(self) -> tuple[str, ...]:
        return self.unresolved_referents.approved_hangul_terms

    def evaluate_provider_candidate(
        self, candidate: str | None
    ) -> ProviderProtectionEvaluation:
        value = candidate or ""
        semantic_passed, semantic_reason = (
            self.semantic_terms.evaluate_provider_candidate(value)
        )
        semantic_restored = (
            self.semantic_terms.restore_provider_candidate(value)
            if semantic_passed
            else value
        )
        unresolved = self.unresolved_referents.evaluate_provider_candidate(
            semantic_restored
        )
        restored = (
            self.unresolved_referents.restore_provider_candidate(semantic_restored)
            if semantic_passed and unresolved.passed
            else semantic_restored
        )
        reason = semantic_reason if not semantic_passed else unresolved.reason
        return ProviderProtectionEvaluation(
            passed=semantic_passed and unresolved.passed,
            reason=reason,
            restored_candidate=restored,
            semantic_passed=semantic_passed,
            semantic_reason=semantic_reason,
            unresolved_evaluation=unresolved,
        )

    def restore_provider_candidate(self, candidate: str) -> str:
        restored = self.unresolved_referents.restore_provider_candidate(candidate)
        return self.semantic_terms.restore_provider_candidate(restored)

    def evaluate_final(self, candidate: str | None) -> FinalProtectionEvaluation:
        unresolved = self.unresolved_referents.evaluate_final(candidate)
        if not unresolved.passed:
            return FinalProtectionEvaluation(False, unresolved.reason)
        semantic_passed, semantic_reason = self.semantic_terms.evaluate_final(candidate)
        return FinalProtectionEvaluation(semantic_passed, semantic_reason)


def _protection_identity(
    provider_source: str,
    spans: tuple[ProtectedSourceSpan, ...],
) -> str:
    payload = {
        "version": _PROTECTION_POLICY_VERSION,
        "provider_source": provider_source,
        "spans": [span.identity_row() for span in spans],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_request_protection(
    source: str,
    *,
    resolved_spans: tuple[ProtectedSourceSpan, ...] = (),
    known_source_spans: tuple[tuple[int, int], ...] = (),
) -> RequestProtection:
    """Resolve every deterministic rendering owner before any provider call."""
    unresolved = resolve_unknown_name_escrow(
        source,
        known_source_spans=known_source_spans,
    )
    semantic = resolve_semantic_terminology(unresolved.provider_source)

    protected: list[ProtectedSourceSpan] = list(resolved_spans)
    protected.extend(
        ProtectedSourceSpan(
            owner="unresolved_referent",
            source_text=entry.source_name,
            source_spans=entry.source_spans,
            rendering=entry.source_name,
            rule_id="unknown_name_escrow",
            placeholder=entry.placeholder,
        )
        for entry in unresolved.entries
    )
    for term in semantic.terms:
        starts: list[tuple[int, int]] = []
        start = source.find(term.source_text)
        while start >= 0:
            starts.append((start, start + len(term.source_text)))
            start = source.find(term.source_text, start + 1)
        source_spans = tuple(starts) if len(starts) == 1 else ()
        protected.append(ProtectedSourceSpan(
            owner="semantic_term",
            source_text=term.source_text,
            source_spans=source_spans,
            rendering=term.target_text,
            rule_id=term.rule_id,
            placeholder=term.placeholder,
        ))

    spans = tuple(protected)
    return RequestProtection(
        original_source=source,
        provider_source=semantic.provider_source,
        spans=spans,
        unresolved_referents=unresolved,
        semantic_terms=semantic,
        identity=_protection_identity(semantic.provider_source, spans),
    )
