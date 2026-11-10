"""One request-local ownership contract for deterministic source spans.

Resolved entities enter this contract before a provider request is built and
are reused unchanged by every route.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


PROTECTION_POLICY_VERSION = "request-source-protection-v2"
_PROTECTION_POLICY_VERSION = PROTECTION_POLICY_VERSION
_HONORIFIC_PARTICLE = r"(?:께서|에게|보다|처럼|하고|한테|은|는|이|가|을|를|께|도|와|과|랑|의)"
_NON_HANGUL_OR_END = r"(?=$|[^\uac00-\ud7a3])"
_SOURCE_HONORIFIC_BOUNDARY = (
    rf"(?=$|[^\uac00-\ud7a3]|{_HONORIFIC_PARTICLE}{_NON_HANGUL_OR_END})"
)
_OPTIONAL_HONORIFIC_PARTICLE = rf"{_HONORIFIC_PARTICLE}?"
# The provider prompt still names ``__LT_UNK_n__``/``__LT_SEM_n__`` and models
# occasionally imitate that syntax (runs 20260916/20260920/20261009).  Nothing
# issues placeholders any more, so any such residue is unrestorable.  Loose on
# purpose: index 0, the literal ``n``, ``LT_UNKNOWN`` and stripped underscores.
_INVENTED_UNKNOWN_PLACEHOLDER_RE = re.compile(r"(?<![A-Za-z])LT_UNK")
_INVENTED_UNKNOWN_PLACEHOLDER_REASON = "unknown_name_invented_placeholder"
_INVENTED_SEMANTIC_PLACEHOLDER_REASON = "semantic_terminology_placeholder_cardinality"


def _invented_placeholder_reason(value: str) -> str:
    if "__LT_SEM_" in value:
        return _INVENTED_SEMANTIC_PLACEHOLDER_REASON
    if _INVENTED_UNKNOWN_PLACEHOLDER_RE.search(value):
        return _INVENTED_UNKNOWN_PLACEHOLDER_REASON
    return ""


def _contains_hangul_syllable(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)


@dataclass(frozen=True)
class ProtectedSourceSpan:
    """A source span whose rendering owner is deterministic for this request."""

    owner: str
    source_text: str
    source_spans: tuple[tuple[int, int], ...]
    rendering: str
    rule_id: str = ""
    placeholder: str = ""
    source_honorifics: tuple[str, ...] = ()

    def identity_row(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "source_text": self.source_text,
            "source_spans": [list(span) for span in self.source_spans],
            "rendering": self.rendering,
            "rule_id": self.rule_id,
            "placeholder": self.placeholder,
            "source_honorifics": list(self.source_honorifics),
        }


@dataclass(frozen=True)
class ProviderProtectionEvaluation:
    passed: bool
    reason: str
    restored_candidate: str


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
    identity: str

    @property
    def active(self) -> bool:
        return bool(self.spans)

    @property
    def fingerprint_identity(self) -> str:
        return self.identity if self.active else ""

    def _normalize_resolved_entity_honorifics(self, candidate: str) -> str:
        """Remove only source-proven Korean honorific residue from canonicals.

        A reviewed Hangul canonical is allowed to remain in a zh-TW subtitle,
        but a provider may copy an adjacent Korean ``님``/``씨`` as part of the
        name.  The canonical owner also owns that exact source morphology for
        this request.  Limit replacements to the number observed in the source
        so this cannot become a general Hangul cleanup path.
        """
        normalized = candidate
        for span in self.spans:
            if (
                span.owner != "resolved_entity"
                or not span.source_honorifics
                or not _contains_hangul_syllable(span.rendering)
            ):
                continue
            for honorific in span.source_honorifics:
                pattern = re.compile(
                    rf"{re.escape(span.rendering)}\s*{re.escape(honorific)}"
                    + _OPTIONAL_HONORIFIC_PARTICLE
                    + _NON_HANGUL_OR_END
                )
                normalized = pattern.sub(span.rendering, normalized, count=1)
        return normalized

    def evaluate_provider_candidate(
        self, candidate: str | None
    ) -> ProviderProtectionEvaluation:
        value = self._normalize_resolved_entity_honorifics(candidate or "")
        reason = _invented_placeholder_reason(value)
        return ProviderProtectionEvaluation(
            passed=not reason,
            reason=reason,
            restored_candidate=value,
        )

    def evaluate_final(self, candidate: str | None) -> FinalProtectionEvaluation:
        reason = _invented_placeholder_reason(candidate or "")
        return FinalProtectionEvaluation(not reason, reason)


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
) -> RequestProtection:
    """Resolve every deterministic rendering owner before any provider call."""
    resolved_with_morphology: list[ProtectedSourceSpan] = []
    for span in resolved_spans:
        honorifics: list[str] = []
        for _, end in span.source_spans:
            match = re.match(
                r"\s*(님|씨)" + _SOURCE_HONORIFIC_BOUNDARY,
                source[end:],
            )
            if match:
                honorifics.append(match.group(1))
        resolved_with_morphology.append(ProtectedSourceSpan(
            owner=span.owner,
            source_text=span.source_text,
            source_spans=span.source_spans,
            rendering=span.rendering,
            rule_id=span.rule_id,
            placeholder=span.placeholder,
            source_honorifics=tuple(honorifics),
        ))
    spans = tuple(resolved_with_morphology)
    return RequestProtection(
        original_source=source,
        provider_source=source,
        spans=spans,
        identity=_protection_identity(source, spans),
    )
