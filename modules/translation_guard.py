"""Output checks moved out of translator.py: placeholder/meta-garbage/refusal detection,
candidate adjudication, the output guard and its copied-name rescue, profile QA and the
untranslated-output check. Behaviour is unchanged; translator.py re-exports these names."""
import re
from dataclasses import dataclass
from config import cfg
from modules.activity_context import effective_profile_id
from modules.profile_context import effective_profile_applied
from utils.runtime_events import (
    _distinct_bigram_ratio,
    source_proven_quality_terms,
    translation_quality,
)
from modules.translation_prompts import get_translation_profile_output_terms
from modules.translation_engines import TranslationEngine
from modules.forensics_contract import ADJUDICATION_POLICY_VERSION, sha256_text
from modules.request_protection import RequestProtection
from modules.translation_corrections import (
    CanonicalObligation,
    CanonicalObligationEvaluation,
    evaluate_canonical_obligations,
)
from modules.translation_normalization import (
    _COMMON_PRESERVED_ACRONYMS,
    _CORRECTION_PREVIEW,
    _LAST_CORRECTIONS,
    _apply_source_aware_corrections,
    _canonical_obligations_for_request,
    _contains_hangul,
    _is_hangul_syllable,
    _normalize_deepseek_taiwan_script,
    _preview_source_aware_corrections,
    _profile_preserve_as_is_terms,
    _publication_approved_terms,
    _request_protection_for,
    _resolve_entity_request_context,
    _simplified_chinese_evidence,
    _source_activated_name_canonicals,
    _unactivated_registry_targets,
    get_corrections,
)

_MIN_SIMPLIFIED_SCRIPT_EVIDENCE = 2  # Avoid one-character name/variant ambiguity.
_MODEL_REFUSAL_RE = re.compile(
    r"^\s*(?:"
    r"(?:translation|output|target|번역|출력|译文|譯文)\s*[:：]"
    r"|(?:(?:i(?:'m| am)\s+)?sorry[,;:]?\s*(?:but\s+)?)?"
    r"i\s+(?:cannot|can't|can\s+not)\s+"
    r"(?:translate|provide\s+(?:a\s+)?translation)\b"
    r"|i\s+am\s+unable\s+to\s+"
    r"(?:translate|provide\s+(?:a\s+)?translation)\b"
    r"|as\s+an\s+ai\b"
    r"|unable\s+to\s+translate\b"
    r")",
    re.IGNORECASE,
)
_HANGUL_RATIO_THRESHOLD = 0.50  # reject result if >50 % of chars are Hangul syllables
_DOTTED_ACRONYM_RE = re.compile(r"^(?:[A-Za-z]\.){2,}[A-Za-z]?$")
_URL_RE = re.compile(
    r"^(?:(?:https?|ftp)://|www\.)[^\s]+$",
    flags=re.IGNORECASE,
)
_BARE_DOMAIN_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?:[/?#][^\s]*)?$"
)
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_HANDLE_RE = re.compile(r"^[@#][A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{1,127}$")
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:[.,:/-]\d+)*$")
_META_GARBAGE_MARKERS = (
    "無法理解",
    "无法理解",
    "無明確語義",
    "无明确语义",
    "STT亂碼",
    "STT乱碼",
    "STT 垃圾",
    "亂碼",
    "乱码",
    "無意義詞",
    "无意义词",
    "無意義",
    "无意义",
    "省略",
)
_IRISE_PROFILE_ID = "irise"
_IRISE_MUSIC_PART_CUES = (
    "고음",
    "보컬",
    "브릿지",
    "후렴",
    "랩",
    "킬링",
    "안무",
    "녹음",
    "노래",
    "하모니",
)
_IRISE_MUSIC_PART_CUE_SUFFIXES = frozenset(
    ("가", "는", "를", "의", "에서", "에도", "도", "만", "와", "과", "로", "이", "을", "랑")
)
_IRISE_BUSINESS_PART_CUES = ("사업", "담당", "업무", "부서", "조직")
_IRISE_PART_CUE_MAX_GAP = 6
_IRISE_PART_TOKEN_RE = re.compile(
    r"(?<![가-힣])파트"
    r"(?=(?:가|는|를|의|에서|에도|도|만|랑|와|과|로|부터|까지|예요|입니다|라고(?:요)?)?"
    r"(?:$|[^가-힣]))"
)
# Deterministic guard for instruction-echo placeholders (audit §15.4): the
# model sometimes writes the *word* for "reply empty" instead of an empty
# reply (2026-07-11 run: 17x "（留空）", 15 shipped to screen). Bracketed
# forms are never legitimate translations. Bare 留空/空白 CAN be real output
# (비워 둬 → 留空, 공백 → 空白), so bare matching is restricted to phrases
# that cannot be a genuine translation of anything.
_PLACEHOLDER_BRACKETED_RE = re.compile(
    r"^[（(\[【\s]+(?:空|留空|空白|無輸出|无输出|無內容|无内容|空字串|空字符串|"
    r"零個字元|零个字符|沒有輸出|没有输出|無翻譯|无翻译|無輸出內容)"
    r"[）)\]】\s。.!…]*$"
)
_PLACEHOLDER_BARE = frozenset({
    "無輸出", "无输出", "空字串", "空字符串", "零個字元", "零个字符",
    "沒有輸出", "没有输出", "無翻譯", "无翻译",
})
_ZERO_OUTPUT_DIRECTIVES = (
    "輸出零個字元",
    "输出零个字符",
    "輸出 0 個字元",
    "输出 0 个字符",
)


def _looks_like_placeholder_output(result: str) -> bool:
    normalized = " ".join((result or "").split())
    if not normalized:
        return False
    if _PLACEHOLDER_BRACKETED_RE.match(normalized):
        return True
    return normalized.rstrip("。.!…") in _PLACEHOLDER_BARE



def _looks_like_meta_garbage_output(result: str) -> bool:
    normalized = result.strip()
    if not normalized:
        return False
    # Placeholder echo is filtered with the same semantics as meta garbage:
    # no subtitle, no cache/history write, and deliberately NO fallback call —
    # the sources are overwhelmingly noise the model was right to refuse, so
    # a fallback engine would just subtitle the noise literally.
    if _looks_like_placeholder_output(normalized):
        return True
    has_zero_output_directive = any(
        marker in normalized for marker in _ZERO_OUTPUT_DIRECTIVES
    )
    # Older models sometimes wrapped the empty-output directive in a longer
    # explanation instead of returning only the short placeholder. Catch the
    # bracketed form, plus unbracketed forms that also carry a known meta
    # explanation marker. A legitimate technical sentence such as
    # "這個函式會輸出零個字元" remains allowed.
    if has_zero_output_directive and (
        normalized.startswith(("(", "（", "[", "【"))
        or any(marker in normalized for marker in _META_GARBAGE_MARKERS)
    ):
        return True
    if "STT" in normalized.upper() and any(marker in normalized for marker in _META_GARBAGE_MARKERS):
        return True
    if normalized.startswith(("(", "（", "[", "【")) and any(
        marker in normalized for marker in _META_GARBAGE_MARKERS
    ):
        return True
    return False



def _looks_like_model_refusal(result: str) -> bool:
    return bool(_MODEL_REFUSAL_RE.search((result or "").strip()))

# Unknown viewer/donor names are kept in Hangul (the provider prompt asks for
# exactly that), so a source name directly addressed with an honorific may be
# published verbatim. Before this allowance both DeepSeek and Groq were
# rejected as ``unexpected_hangul`` for 랑코님-style names and the whole
# subtitle was lost (18 of 666 DeepSeek-primary sentences, 2026-09-17..10-04).
_HONORIFIC_NAME_RE = re.compile(
    r"(?<![가-힣])([가-힣]{2,10})( ?)(님|씨|언니|오빠|누나|쌤)"
)
# Ordinary words that take the same suffixes; keeping these in Hangul would be
# an untranslated word, not a preserved name. Covers titles/kinship, the
# pronoun 님 after fillers/adverbs, expletive 씨, and subject teachers (쌤).
_HONORIFIC_NON_NAMES = frozenset({
    # titles, roles and kinship
    "선생", "사장", "대장", "회장", "팀장", "부장", "과장", "실장", "원장", "교수",
    "감독", "작가", "기사", "고객", "시청자", "구독자", "관리자", "운영자", "매니저",
    "주인", "회원", "사모", "스승", "도련", "아가", "아저", "마음", "아버", "어머",
    "할머", "할아버", "우리", "저희", "너희", "여러", "친구", "동생", "언니", "오빠",
    "누나", "선배", "후배", "사부", "의사", "간호사", "박사", "기장", "반장", "총장",
    "국장", "본부장", "이사", "대표", "전무", "상무", "편집장", "점장", "방장",
    "후원자", "하나", "하느", "공주", "왕자", "이모", "고모", "삼촌", "숙모", "외삼촌",
    "사촌", "막내", "작은", "큰", "옆집", "동네", "아랫집", "윗집", "친척", "엄마",
    "아빠", "부모", "손님", "형님", "님", "대리", "주임", "차장", "교장",
    "부사장", "변호사", "판사", "검사", "형사", "경찰", "코치", "선수", "목사",
    "신부", "수녀", "부처", "스님", "조상", "임금", "서방", "아드", "따님",
    "담임", "학원", "과외", "택배", "존나", "미친", "아놔",
    # seed nouns + 씨
    "수박", "해바라기", "호박", "사과", "포도", "참외", "고추", "참깨", "들깨",
    # subjects before 쌤
    "영어", "수학", "국어", "과학", "사회", "미술", "음악", "체육", "역사", "보컬",
    "댄스", "노래", "피아노", "기타", "드럼",
    # fillers, adverbs and pronouns that can precede the pronoun 님
    "근데", "그런데", "아니", "그거", "이거", "저거", "그게", "이게", "저게",
    "그러니까", "그니까", "그래서", "그리고", "그럼", "그러면", "모두", "진짜",
    "정말", "너무", "그냥", "혹시", "제발", "저기", "여기", "거기", "이제", "지금",
    "아까", "그래", "아이고", "어머", "대박", "완전", "다들", "모든", "이분", "그분",
    "저분", "당신", "자기", "너네", "얘들", "애들",
    # expletive 씨
    "아이", "에이", "아우", "아오", "이런", "젠장",
})
_KINSHIP_SUFFIXES = frozenset({"언니", "오빠", "누나"})
# Compound titles (담임선생님, 택배기사님) end in an ordinary title.
_HONORIFIC_TITLE_ENDINGS = tuple(sorted(
    {
        "선생", "사장", "기사", "작가", "대장", "회장", "팀장", "부장", "과장",
        "실장", "원장", "교수", "감독", "의사", "박사", "대리", "주임", "차장",
        "교장", "변호사", "판사", "검사", "형사", "코치", "선수", "목사", "신부",
        "매니저", "관리자", "운영자", "시청자", "구독자", "후원자", "방장", "반장",
        "점장", "총장", "국장", "이사", "대표", "할머", "할아버", "언니", "오빠",
        "누나", "분들",
    },
    key=len,
    reverse=True,
))
# 씨 followed by these starts another word (씨발, 씨팔, 씨앗, 씨름), not 氏.
_SSI_WORD_CONTINUATIONS = ("발", "팔", "바", "앗", "름")


def _source_honorific_name_terms(source: str) -> frozenset[str]:
    """Return source-verbatim Hangul names addressed with an honorific."""
    terms: set[str] = set()
    text = source or ""
    for match in _HONORIFIC_NAME_RE.finditer(text):
        name, space, suffix = match.groups()
        if name in _HONORIFIC_NON_NAMES or name.endswith(_HONORIFIC_TITLE_ENDINGS):
            continue
        if suffix == "씨" and text[match.end():match.end() + 1] in _SSI_WORD_CONTINUATIONS:
            continue
        # A spaced suffix is a name only for 님 (``무채석송 님``); ``아는 오빠``
        # and ``진짜 씨`` are a modifier or an expletive, not an address.
        if space and suffix != "님":
            continue
        # Adnominal modifiers (``아는``/``좋아하던``) before kinship words.
        if suffix in _KINSHIP_SUFFIXES and name.endswith(("는", "던")):
            continue
        terms.update((name, f"{name}{suffix}", f"{name} {suffix}"))
    return frozenset(terms)



@dataclass(frozen=True)
class _CandidateAdjudication:
    """One candidate's deterministic restore and publication decision."""

    candidate_output: str
    reason: str
    rejection_owner: str
    canonical_evaluation: CanonicalObligationEvaluation
    evidence: dict[str, object]
    accepted_after_name_render: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.reason



def _repetition_mirrors_source(source: str, target: str) -> bool:
    """The speaker repeated themselves (안 돼 안 돼 안 돼), so a looping target is faithful, not degenerate.

    All 15 ``repetitive_target`` rejections in the October live runs had a source that loops by the same
    measure; the fallback then published the same repetition about a second later, or the line was lost
    (docs/agent/LIVE_PRIMARY_RESCUE_PLAN_20261012.md). Measured on letters only, so a run of "!!!" does
    not make a source repetitive, and a target noticeably longer than its source still fails.
    """
    source_body = _GUARD_RESCUE_IGNORED.sub("", source or "")
    target_body = _GUARD_RESCUE_IGNORED.sub("", target or "")
    return (
        len(source_body) >= 2
        and _distinct_bigram_ratio(source_body) < 0.5
        and len(target_body) <= 1.25 * len(source_body)
    )



def _adjudicate_translation_candidate(
    engine: TranslationEngine | None,
    result: str,
    source: str,
    *,
    obligations: tuple[CanonicalObligation, ...] | None = None,
    request_protection: RequestProtection | None = None,
    record_corrections: bool = False,
    publication: bool = False,
) -> _CandidateAdjudication:
    """Restore and adjudicate one provider/cache/provisional candidate.

    Every provider is judged after deterministic corrections for unexpected
    Hangul/Kana residue. Flash additionally retains its raw-output boundary:
    only exact, source-gated ``name_render`` rules may rescue a raw script
    violation, and only when those rules alone remove all residue of that type.
    """
    if obligations is None:
        entity_context = _resolve_entity_request_context(source)
        obligations = _canonical_obligations_for_request(entity_context)
    if request_protection is None:
        entity_context = _resolve_entity_request_context(source)
        request_protection = _request_protection_for(
            source, entity_context, obligations
        )
    engine_name = str(getattr(engine, "engine_name", "") or "")
    is_deepseek = engine_name == "deepseek"
    protection_evaluation = request_protection.evaluate_provider_candidate(result)
    terminology_passed = protection_evaluation.semantic_passed
    terminology_reason = protection_evaluation.semantic_reason
    escrow_evaluation = protection_evaluation.unresolved_evaluation
    restored_result = protection_evaluation.restored_candidate
    if record_corrections:
        corrected = _apply_source_aware_corrections(source, restored_result)
        corrections = get_corrections()
    else:
        corrected, corrections = _preview_source_aware_corrections(
            source, restored_result
        )
    source_corrected = corrected
    if is_deepseek:
        if record_corrections:
            corrected = _normalize_deepseek_taiwan_script(corrected)
            corrections = get_corrections()
        else:
            preview_token = _CORRECTION_PREVIEW.set(True)
            previous = getattr(_LAST_CORRECTIONS, "value", None)
            _LAST_CORRECTIONS.value = []
            try:
                corrected = _normalize_deepseek_taiwan_script(corrected)
                corrections += get_corrections()
            finally:
                _CORRECTION_PREVIEW.reset(preview_token)
                if previous is None:
                    delattr(_LAST_CORRECTIONS, "value")
                else:
                    _LAST_CORRECTIONS.value = previous
    final_protection_evaluation = request_protection.evaluate_final(corrected)
    unactivated_entity_targets = _unactivated_registry_targets(source, corrected)
    simplified_chinese_spans = _simplified_chinese_evidence(corrected)
    profile_id = (
        effective_profile_id(getattr(cfg, "active_streamer_profile", ""))
        if effective_profile_applied(cfg.translation.use_profile)
        else ""
    )
    approved_terms = set(_publication_approved_terms(profile_id, obligations))
    # Preserve the existing generic source-proof allowance, while removing the
    # old profile-wide Hangul allowlist.  A Korean span absent from this source
    # and from its activated obligations remains a script violation.
    approved_terms.update(source_proven_quality_terms(source))
    approved_terms.update(_source_honorific_name_terms(source))
    approved_terms.update(_source_activated_name_canonicals(source))
    approved_terms.update(request_protection.approved_hangul_terms)
    approved_terms = frozenset(approved_terms)
    obligation_evaluation = evaluate_canonical_obligations(corrected, obligations)
    raw_quality = translation_quality(
        source,
        restored_result,
        approved_terms=approved_terms,
    )
    quality = translation_quality(
        source,
        corrected,
        approved_terms=approved_terms,
    )
    raw_flags = set(raw_quality.get("quality_flags") or [])
    raw_classifications = set(
        raw_quality.get("quality_classifications") or []
    )
    flags = set(quality.get("quality_flags") or [])
    classifications = set(quality.get("quality_classifications") or [])
    raw_script_violations = set()
    if "target_has_japanese" in raw_flags:
        raw_script_violations.add("unexpected_japanese")
    if "target_has_unexpected_hangul" in raw_classifications:
        raw_script_violations.add("unexpected_hangul")
    corrected_script_violations = set()
    if "target_has_japanese" in flags:
        corrected_script_violations.add("unexpected_japanese")
    if "target_has_unexpected_hangul" in classifications:
        corrected_script_violations.add("unexpected_hangul")

    name_render_rescued: set[str] = set()
    if raw_script_violations - corrected_script_violations:
        name_rendered, name_render_corrections = _preview_source_aware_corrections(
            source,
            restored_result,
            name_render_only=True,
        )
        name_render_quality = translation_quality(
            source,
            name_rendered,
            approved_terms=approved_terms,
        )
        name_render_flags = set(name_render_quality.get("quality_flags") or [])
        name_render_classifications = set(
            name_render_quality.get("quality_classifications") or []
        )
        name_render_violations = set()
        if "target_has_japanese" in name_render_flags:
            name_render_violations.add("unexpected_japanese")
        if "target_has_unexpected_hangul" in name_render_classifications:
            name_render_violations.add("unexpected_hangul")
        if any(
            correction.get("stage") == "name_render"
            for correction in name_render_corrections
        ):
            name_render_rescued = (
                raw_script_violations
                - corrected_script_violations
                - name_render_violations
            )

    degenerate_repetition = (
        is_deepseek
        and "repetitive_target" in flags
        and not _repetition_mirrors_source(source, corrected)
    )
    reason = ""
    rejection_owner = ""
    publication_meta_rejection = publication and (
        _looks_like_meta_garbage_output(corrected)
        or _looks_like_model_refusal(corrected)
    )
    if publication_meta_rejection:
        # Preserve the historical finalizer contract: a non-translation is
        # filtered as model output even when it also violates another invariant.
        reason = "meta_garbage_output"
        rejection_owner = "model_output"
    elif not terminology_passed:
        reason = terminology_reason
        rejection_owner = "request_protection"
    elif not escrow_evaluation.passed:
        reason = escrow_evaluation.reason
        rejection_owner = "request_protection"
    elif not final_protection_evaluation.passed:
        reason = final_protection_evaluation.reason
        rejection_owner = "request_protection"
    elif unactivated_entity_targets:
        reason = "unactivated_entity_target"
        rejection_owner = "entity_provenance"
    elif len(simplified_chinese_spans) >= _MIN_SIMPLIFIED_SCRIPT_EVIDENCE:
        reason = "simplified_chinese"
        rejection_owner = "script_safety"
    elif not obligation_evaluation.passed:
        reason = "canonical_obligation_missing"
        rejection_owner = "canonical_obligation"
    elif "unexpected_japanese" in corrected_script_violations:
        reason = "unexpected_japanese"
        rejection_owner = "script_safety"
    elif "unexpected_hangul" in corrected_script_violations:
        reason = "unexpected_hangul"
        rejection_owner = "script_safety"
    elif is_deepseek and "unexpected_japanese" in (
        raw_script_violations - name_render_rescued
    ):
        reason = "unexpected_japanese"
        rejection_owner = "script_safety"
    elif is_deepseek and "unexpected_hangul" in (
        raw_script_violations - name_render_rescued
    ):
        reason = "unexpected_hangul"
        rejection_owner = "script_safety"
    elif is_deepseek and _looks_like_meta_garbage_output(corrected):
        reason = "meta_garbage_output"
        rejection_owner = "model_output"
    elif is_deepseek and _looks_like_model_refusal(corrected):
        reason = "model_refusal"
        rejection_owner = "model_output"
    elif is_deepseek and "target_meta_leak" in flags:
        reason = "target_meta_leak"
        rejection_owner = "model_output"
    elif degenerate_repetition:
        reason = "repetitive_target"
        rejection_owner = "model_output"
    failed_invariants: list[dict[str, str]] = []
    def add_failure(condition: bool, failure_reason: str, owner: str) -> None:
        if condition and not any(
            row["reason"] == failure_reason and row["owner"] == owner
            for row in failed_invariants
        ):
            failed_invariants.append({"reason": failure_reason, "owner": owner})

    add_failure(publication_meta_rejection, "meta_garbage_output", "model_output")
    add_failure(not terminology_passed, terminology_reason, "request_protection")
    add_failure(not escrow_evaluation.passed, escrow_evaluation.reason, "request_protection")
    add_failure(not final_protection_evaluation.passed, final_protection_evaluation.reason, "request_protection")
    add_failure(bool(unactivated_entity_targets), "unactivated_entity_target", "entity_provenance")
    add_failure(len(simplified_chinese_spans) >= _MIN_SIMPLIFIED_SCRIPT_EVIDENCE, "simplified_chinese", "script_safety")
    add_failure(not obligation_evaluation.passed, "canonical_obligation_missing", "canonical_obligation")
    add_failure("unexpected_japanese" in corrected_script_violations, "unexpected_japanese", "script_safety")
    add_failure("unexpected_hangul" in corrected_script_violations, "unexpected_hangul", "script_safety")
    add_failure(is_deepseek and "unexpected_japanese" in (raw_script_violations - name_render_rescued), "raw_unexpected_japanese", "script_safety")
    add_failure(is_deepseek and "unexpected_hangul" in (raw_script_violations - name_render_rescued), "raw_unexpected_hangul", "script_safety")
    add_failure(is_deepseek and _looks_like_meta_garbage_output(corrected), "meta_garbage_output", "model_output")
    add_failure(is_deepseek and _looks_like_model_refusal(corrected), "model_refusal", "model_output")
    add_failure(is_deepseek and "target_meta_leak" in flags, "target_meta_leak", "model_output")
    add_failure(degenerate_repetition, "repetitive_target", "model_output")
    evidence = {
        "version": 2,
        "policy_version": ADJUDICATION_POLICY_VERSION,
        "disposition": "rejected" if reason else "accepted",
        "primary_reason": reason,
        "rejection_owner": rejection_owner,
        "failed_invariants": failed_invariants,
        "candidate_stages": {
            "raw_provider": {"text": result, "sha256": sha256_text(result)},
            "protection_restored": {"text": restored_result, "sha256": sha256_text(restored_result)},
            "source_corrected": {"text": source_corrected, "sha256": sha256_text(source_corrected)},
            "script_normalized": {"text": corrected, "sha256": sha256_text(corrected)},
        },
        "candidate_raw_output": result,
        "candidate_output": corrected,
        "candidate_corrections": corrections,
        "candidate_quality_flags": sorted(flags),
        "candidate_quality_classifications": sorted(classifications),
        "candidate_raw_quality_flags": sorted(raw_flags),
        "candidate_raw_quality_classifications": sorted(raw_classifications),
        "canonical_obligations": obligation_evaluation.as_dict(),
        "unactivated_entity_targets": list(unactivated_entity_targets),
        "simplified_chinese_spans": list(simplified_chinese_spans),
        "semantic_terminology": {
            "active": request_protection.semantic_terms.active,
            "rule_ids": [
                term.rule_id for term in request_protection.semantic_terms.terms
            ],
            "passed": terminology_passed,
        },
        "unknown_name_escrow": {
            "active": request_protection.unresolved_referents.active,
            "expected": list(escrow_evaluation.expected),
            "missing": list(escrow_evaluation.missing),
            "duplicated": list(escrow_evaluation.duplicated),
            "mutated_placeholder": escrow_evaluation.mutated_placeholder,
            "invented_aliases": list(escrow_evaluation.invented_aliases),
            "approved_hangul_terms": list(
                request_protection.approved_hangul_terms
            ),
        },
        "request_protection": {
            "identity": request_protection.identity,
            "active": request_protection.active,
            "spans": [span.identity_row() for span in request_protection.spans],
        },
    }
    if reason:
        evidence["reason"] = reason
    elif name_render_rescued:
        evidence["accepted_after_name_render"] = sorted(name_render_rescued)
    return _CandidateAdjudication(
        candidate_output=corrected,
        reason=reason,
        rejection_owner=rejection_owner,
        canonical_evaluation=obligation_evaluation,
        evidence=evidence,
        accepted_after_name_render=tuple(sorted(name_render_rescued)),
    )



def _translation_output_guard(
    engine: TranslationEngine | None,
    result: str,
    source: str,
    *,
    obligations: tuple[CanonicalObligation, ...] | None = None,
    request_protection: RequestProtection | None = None,
) -> dict[str, object]:
    """Compatibility adapter exposing adjudication to fallback telemetry."""
    adjudication = _adjudicate_translation_candidate(
        engine,
        result,
        source,
        obligations=obligations,
        request_protection=request_protection,
    )
    if (
        not adjudication.reason
        and not adjudication.accepted_after_name_render
        and str(getattr(engine, "engine_name", "") or "") != "deepseek"
    ):
        return {}
    return adjudication.evidence



def _quality_telemetry_approved_terms(
    profile_id: str,
    source_text: str,
    obligations: tuple[CanonicalObligation, ...] = (),
) -> frozenset[str]:
    """Add diagnostic-only evidence without changing publication policy."""
    terms = set(_publication_approved_terms(profile_id, obligations))
    if profile_id:
        terms.update(
            term
            for term in get_translation_profile_output_terms(profile_id)
            if not _contains_hangul(term)
        )
    terms.update(source_proven_quality_terms(source_text))
    terms.update(_source_honorific_name_terms(source_text))
    if profile_id:
        terms.update(
            _source_activated_name_canonicals(
                source_text,
                profile_id=profile_id,
            )
        )
    return frozenset(term for term in terms if term)

# --- Last-resort "copied source name" rescue (docs/agent/LIVE_DROPPED_SUBTITLES_PLAN_20261010.md) ---
# When every route was rejected, a candidate whose only problem is Hangul copied from the source as a
# name-like token is published instead of losing the whole subtitle (user decision 2026-10-10).
GUARD_RESCUE_POLICY = "copied_source_name_v2"
# v2 (docs/agent/LIVE_RESCUE_V2_PLAN_20261011.md): up to four distinct runs, and a run may be part of one
# source word (유천 from 유천이) when it has at least two syllables. Ratio and ending checks unchanged.
_GUARD_RESCUE_MAX_RUNS = 4
_GUARD_RESCUE_MIN_SUBSTRING = 2
# Pieces only: a source word in a connective/adnominal verb form (예쁘게, 좋아하는 -> 좋아하) is a
# predicate even though the final-ending list misses it, so its stem is not a copied name.
_GUARD_RESCUE_PIECE_WORD_ENDINGS = ("게", "하", "던", "기")
_GUARD_RESCUE_REASONS = frozenset({"unexpected_hangul", "raw_unexpected_hangul"})
_GUARD_RESCUE_PARTICLES = tuple(sorted(
    ("야", "아", "이", "가", "은", "는", "을", "를", "의", "도", "랑", "이랑", "한테", "에게", "이다", "다"),
    key=len, reverse=True,
))
# A run ending like a predicate is a copied clause, not a name (round-3 contract). Checked on the run itself
# and on the run minus one trailing particle. Short names that happen to end the same way (민지, 은서) are
# simply not rescued — the safe direction.
_GUARD_RESCUE_PREDICATE_ENDINGS = tuple(sorted(
    ("요", "다", "니다", "니", "까", "죠", "네", "지", "고", "서", "면", "는데", "어서", "아서",
     "었", "았", "했", "됐", "해", "돼", "세요", "습니다"),
    key=len, reverse=True,
))
_GUARD_RESCUE_HANGUL_RUN = re.compile(r"[\uac00-\ud7a3]+")
_GUARD_RESCUE_HAN = re.compile(r"[\u4e00-\u9fff]")
_GUARD_RESCUE_IGNORED = re.compile(r"[\s\W_]+")


def _guard_rescue_name_tokens(source: str) -> frozenset[str]:
    tokens: set[str] = set()
    for raw in (source or "").split():
        token = re.sub(r"^[^\w]+|[^\w]+$", "", raw)  # edge punctuation only; letters/digits/Han stay
        if not token:
            continue
        tokens.add(token)
        for particle in _GUARD_RESCUE_PARTICLES:
            if token.endswith(particle) and len(token) > len(particle):
                tokens.add(token[: -len(particle)])
                break
    return frozenset(tokens)



def _guard_rescue_run_from_source(run: str, source: str, tokens: frozenset[str]) -> bool:
    """A whole source token (one particle stripped), or a 2+ syllable piece of one source word.

    A piece only counts when its source word is not itself predicate-like, so 유천 from 유천이
    qualifies but 먹었습 from 먹었습니다, 예쁘 from 예쁘게 and 좋아 from 좋아하는 do not.
    """
    if run in tokens:
        return True
    if len(run) < _GUARD_RESCUE_MIN_SUBSTRING:
        return False
    for raw in (source or "").split():
        word = re.sub(r"^[^\w]+|[^\w]+$", "", raw)
        if run in word and not _guard_rescue_predicate_like(word) and not _guard_rescue_verb_form_word(word):
            return True
    return False



def _guard_rescue_verb_form_word(word: str) -> bool:
    forms = {word}
    for particle in _GUARD_RESCUE_PARTICLES:
        if word.endswith(particle) and len(word) > len(particle):
            forms.add(word[: -len(particle)])
            break
    return any(form.endswith(_GUARD_RESCUE_PIECE_WORD_ENDINGS) for form in forms)



def _guard_rescue_predicate_like(run: str) -> bool:
    forms = {run}
    for particle in _GUARD_RESCUE_PARTICLES:
        if run.endswith(particle) and len(run) > len(particle):
            forms.add(run[: -len(particle)])
            break
    return any(form.endswith(_GUARD_RESCUE_PREDICATE_ENDINGS) for form in forms)



def _guard_rescue_hangul_ratio(text: str) -> float:
    body = _GUARD_RESCUE_IGNORED.sub("", text or "")
    return len("".join(_GUARD_RESCUE_HANGUL_RUN.findall(text or ""))) / len(body) if body else 1.0



def _guard_rescue_text_ok(text: str | None, source: str) -> bool:
    """Every Hangul run is a name-like token or word piece of this source; the text is mostly Han and not a copy."""
    if not text or re.sub(r"\s+", "", text) == re.sub(r"\s+", "", source or ""):
        return False
    runs = _GUARD_RESCUE_HANGUL_RUN.findall(text)
    body = _GUARD_RESCUE_IGNORED.sub("", text)
    if not body:
        return False
    if runs:
        tokens = _guard_rescue_name_tokens(source)
        distinct = set(runs)
        if len(distinct) > _GUARD_RESCUE_MAX_RUNS or any(
            len(run) > 8
            or not _guard_rescue_run_from_source(run, source, tokens)
            or _guard_rescue_predicate_like(run)
            for run in distinct
        ):
            return False
        if sum(len(run) for run in runs) / len(body) > 0.30:
            return False
    return len(_GUARD_RESCUE_HAN.findall(text)) / len(body) >= 0.50



def _guard_rescue_eligible(evidence: object, source: str) -> bool:
    """Adjudication evidence whose only failures are Hangul script checks, with both stages copied-name-only."""
    if not isinstance(evidence, dict) or evidence.get("disposition") != "rejected":
        return False
    failed = evidence.get("failed_invariants") or []
    if not failed or any(
        not isinstance(row, dict) or row.get("owner") != "script_safety"
        or row.get("reason") not in _GUARD_RESCUE_REASONS
        for row in failed
    ):
        return False
    stages = evidence.get("candidate_stages") if isinstance(evidence.get("candidate_stages"), dict) else {}
    restored = (stages.get("protection_restored") or {}).get("text") if isinstance(stages.get("protection_restored"), dict) else None
    corrected = evidence.get("candidate_output")
    if not isinstance(restored, str) or not isinstance(corrected, str):
        return False  # without both stages there is no proof; never rescue
    return _guard_rescue_text_ok(restored, source) and _guard_rescue_text_ok(corrected, source)



def _choose_guard_rescue(attempts: tuple[dict[str, object], ...], source: str) -> dict[str, object] | None:
    """Pick the eligible rejected attempt with the least Hangul (ties: earliest); None when none qualifies."""
    best: tuple[float, int] | None = None
    for index, attempt in enumerate(attempts):
        if attempt.get("status") != "rejected_output":
            continue
        evidence = attempt.get("output_guard")
        if not _guard_rescue_eligible(evidence, source):
            continue
        ratio = _guard_rescue_hangul_ratio(str(evidence.get("candidate_output") or ""))
        if best is None or ratio < best[0]:
            best = (ratio, index)
    if best is None:
        return None
    return {"policy": GUARD_RESCUE_POLICY, "attempt_index": best[1]}



def _final_script_rejection_reason(event_fields: dict[str, object]) -> str:
    """Return the authoritative publication-time script violation, if any."""
    classifications = set(event_fields.get("quality_classifications") or ())
    flags = set(event_fields.get("quality_flags") or ())
    rescued_names_only = (
        event_fields.get("result_source") == "guard_rescue"
        and _guard_rescue_text_ok(
            str(event_fields.get("target_text") or ""),
            str(event_fields.get("prepared_source_text") or event_fields.get("source_text") or ""),
        )
    )
    if "target_has_unexpected_hangul" in classifications and not rescued_names_only:
        return "unexpected_hangul"
    if "target_has_japanese" in flags:
        return "unexpected_japanese"
    return ""



def _bounded_korean_term_spans(
    text: str,
    terms: tuple[str, ...],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in terms:
        search_from = 0
        while True:
            start = text.find(term, search_from)
            if start < 0:
                break
            end = start + len(term)
            search_from = start + 1
            if start > 0 and _is_hangul_syllable(text[start - 1]):
                continue
            if end < len(text) and _is_hangul_syllable(text[end]):
                suffix_end = end
                while suffix_end < len(text) and _is_hangul_syllable(text[suffix_end]):
                    suffix_end += 1
                if text[end:suffix_end] not in _IRISE_MUSIC_PART_CUE_SUFFIXES:
                    continue
            spans.append((start, end))
    return spans



def _irise_music_part_semantic_candidate(source_text: str, target_text: str) -> bool:
    """Conservatively flag music/performance `파트` rendered as `部門`.

    This is diagnostic-only.  The local cue requirement intentionally leaves
    ambiguous business/team uses alone, and the token matcher excludes words
    such as `파트너`.
    """
    if "部門" not in target_text:
        return False

    for clause in re.split(r"[.!?。！？\n]+", source_text or ""):
        part_matches = list(_IRISE_PART_TOKEN_RE.finditer(clause))
        if not part_matches:
            continue
        if "저희 파트" in clause and "수정" in clause:
            return True
        cue_spans = _bounded_korean_term_spans(clause, _IRISE_MUSIC_PART_CUES)
        for part_match in part_matches:
            local_start = max(0, part_match.start() - _IRISE_PART_CUE_MAX_GAP)
            local_end = min(len(clause), part_match.end() + _IRISE_PART_CUE_MAX_GAP)
            local_text = clause[local_start:local_end]
            if any(cue in local_text for cue in _IRISE_BUSINESS_PART_CUES):
                continue
            for cue_start, cue_end in cue_spans:
                gap = max(
                    part_match.start() - cue_end,
                    cue_start - part_match.end(),
                    0,
                )
                if gap <= _IRISE_PART_CUE_MAX_GAP:
                    return True
    return False



def _profile_translation_qa(
    *,
    source_text: str,
    target_text: str | None,
    status: str,
    incomplete: bool,
    profile_id: str,
    profile_applied: bool,
    metadata: dict,
    quality: dict,
    obligation_evaluation: CanonicalObligationEvaluation | None = None,
) -> dict:
    """Return profile-scoped QA evidence without changing retry or routing.

    Deterministic repairs remain attributable through the existing
    `corrections` trace.  This helper only adds post-correction obligations and
    diagnostic suspicions; it never mutates legacy quality flags or scores.
    """
    # Canonical QA and hard publication acceptance share one resolver.  Keep
    # the legacy flat fields while avoiding a second profile-specific mapping.
    expected_terms = list(
        obligation_evaluation.expected if obligation_evaluation is not None else ()
    )

    eligible = status == "success" and not incomplete and target_text is not None
    target = target_text or ""
    missing_terms = list(
        obligation_evaluation.missing
        if eligible and obligation_evaluation is not None
        else ()
    )

    semantic_candidates: list[str] = []
    if (
        eligible
        and profile_applied
        and profile_id == _IRISE_PROFILE_ID
        and _irise_music_part_semantic_candidate(source_text, target)
    ):
        semantic_candidates.append("music_part_rendered_as_department")

    qa_flags: list[str] = []
    classifications = list(quality.get("quality_classifications") or [])
    if missing_terms:
        qa_flags.append("target_missing_profile_canonical")
        classifications.append("target_missing_profile_canonical")
    if semantic_candidates:
        qa_flags.append("target_profile_semantic_candidate")
        classifications.append("target_profile_semantic_candidate")

    # Script drift already has mature generic evidence in translation_quality;
    # mirror it only in the disposition so this profile QA remains additive.
    has_script_suspicion = (
        "target_has_japanese" in (quality.get("quality_flags") or [])
        or "target_has_unexpected_hangul" in classifications
    )
    name_render_fixed = any(
        isinstance(correction, dict)
        and correction.get("stage") == "name_render"
        and correction.get("after") in expected_terms
        for correction in (metadata.get("corrections") or [])
    )
    if qa_flags or has_script_suspicion:
        disposition = "suspicious"
    elif name_render_fixed:
        disposition = "normalized"
    else:
        disposition = "clean"

    return {
        "target_expected_canonical_terms": expected_terms,
        "target_missing_canonical_terms": missing_terms,
        "target_profile_semantic_candidates": semantic_candidates,
        "translation_qa_flags": qa_flags,
        "translation_qa_disposition": disposition,
        "quality_classifications": list(dict.fromkeys(classifications)),
    }



def _is_legitimate_preserve_as_is(source: str) -> bool:
    """Return True only when an identical translation is provably intentional."""
    text = (source or "").strip()
    if not text or "\n" in text or "\r" in text:
        return False

    if text in _profile_preserve_as_is_terms():
        return True
    if text in _COMMON_PRESERVED_ACRONYMS:
        return True
    if _DOTTED_ACRONYM_RE.fullmatch(text):
        return True
    if (
        _URL_RE.fullmatch(text)
        or _BARE_DOMAIN_RE.fullmatch(text)
        or _EMAIL_RE.fullmatch(text)
        or _HANDLE_RE.fullmatch(text)
    ):
        return True
    if _NUMERIC_LITERAL_RE.fullmatch(text):
        return True
    if _MACHINE_ID_RE.fullmatch(text):
        has_letter = any(char.isascii() and char.isalpha() for char in text)
        has_digit = any(char.isdigit() for char in text)
        if "_" in text or has_letter and has_digit:
            return True
    return False



def _looks_untranslated(result: str, source: str) -> bool:
    if result.strip() == source.strip():
        return not _is_legitimate_preserve_as_is(source)
    chars = [c for c in result if not c.isspace()]
    if not chars:
        return False
    if len(chars) < 6:
        return False  # too short for ratio to be meaningful (single Korean name is OK)
    hangul = sum(1 for c in chars if "가" <= c <= "힣")
    if (hangul / len(chars)) > _HANGUL_RATIO_THRESHOLD:
        return True
    # Japanese hiragana/katakana should never appear in zh-TW output
    japanese = sum(1 for c in chars if "぀" <= c <= "ゟ" or "゠" <= c <= "ヿ")
    if japanese > 2:
        return True
    # Result much longer than source likely means hallucinated continuation
    src_chars = len([c for c in source if not c.isspace()])
    if len(chars) > src_chars * 3 and len(chars) > 40:
        return True
    return False
