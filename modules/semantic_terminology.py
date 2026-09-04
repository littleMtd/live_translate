"""Source-grounded escrow for a deliberately small semantic terminology set.

This module does not identify people and does not participate in canonical-name
resolution.  A rule activates only from an exact Korean source form, protects
that meaning with an opaque provider token, and restores one fixed zh-TW
rendering after the provider preserves the token exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_PLACEHOLDER_RE = re.compile(r"__LT_SEM_[1-9][0-9]*__")


@dataclass(frozen=True)
class SemanticTerm:
    rule_id: str
    source_text: str
    target_text: str
    placeholder: str


@dataclass(frozen=True)
class SemanticTerminologyEscrow:
    original_source: str
    provider_source: str
    terms: tuple[SemanticTerm, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.terms)

    def evaluate_provider_candidate(self, candidate: str | None) -> tuple[bool, str]:
        text = candidate or ""
        for term in self.terms:
            count = text.count(term.placeholder)
            if count != 1:
                return False, "semantic_terminology_placeholder_cardinality"
            text = text.replace(term.placeholder, "")
        if _PLACEHOLDER_RE.search(text) or "__LT_SEM_" in text:
            return False, "semantic_terminology_placeholder_cardinality"
        return True, ""

    def restore_provider_candidate(self, candidate: str) -> str:
        restored = candidate
        for term in self.terms:
            restored = restored.replace(term.placeholder, term.target_text)
        return restored

    def evaluate_final(self, candidate: str | None) -> tuple[bool, str]:
        text = candidate or ""
        for term in self.terms:
            if text.count(term.target_text) != 1:
                return False, "semantic_terminology_final_cardinality"
        if _PLACEHOLDER_RE.search(text) or "__LT_SEM_" in text:
            return False, "semantic_terminology_final_cardinality"
        return True, ""


_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "wuthering_waves_game_title",
        re.compile(r"(?<![가-힣])명조(?![가-힣])"),
        "鳴潮",
    ),
    (
        "sapa_antisocial",
        re.compile(r"(?<![가-힣])사패(?=$|[^가-힣]|[이가은는을를도만과와로에])"),
        "反社會人格",
    ),
    (
        "nickgab_live_up_to_nickname",
        re.compile(
            r"(?<![가-힣])닉값(?:\s*(?:을\s*)?"
            r"하(?:시면|세요|면|는|고|지|다|게|기|신|셨|는지))?(?![가-힣])"
        ),
        "名副其實",
    ),
    (
        "jjam_dump_work",
        re.compile(
            r"(?<![가-힣])짬\s*(?:을\s*)?"
            r"(?:때리(?:다|는|고|면|지|기|게|신|셨|는지)?|"
            r"때린|때릴|때려|때렸(?:다|어|으면)?)(?![가-힣])"
        ),
        "把事情丟給別人",
    ),
    (
        "amplification_release",
        re.compile(
            r"(?<![가-힣])증폭(?:을)?\s+"
            r"풀어주시면(?:\s+풀어주시면)?(?![가-힣])"
        ),
        "解除增幅",
    ),
)


def resolve_semantic_terminology(source: str) -> SemanticTerminologyEscrow:
    """Resolve at most one exact occurrence of each v1 semantic term."""
    matches: list[tuple[int, int, str, str, str]] = []
    for rule_id, pattern, target in _RULES:
        found = list(pattern.finditer(source))
        if len(found) != 1:
            continue
        match = found[0]
        matches.append((match.start(), match.end(), rule_id, match.group(0), target))

    if not matches:
        return SemanticTerminologyEscrow(source, source)

    matches.sort()
    provider = source
    terms: list[SemanticTerm] = []
    for index, (start, end, rule_id, matched, target) in reversed(
        list(enumerate(matches, start=1))
    ):
        placeholder = f"__LT_SEM_{index}__"
        provider = provider[:start] + placeholder + provider[end:]
        terms.append(SemanticTerm(rule_id, matched, target, placeholder))
    terms.reverse()
    return SemanticTerminologyEscrow(source, provider, tuple(terms))
