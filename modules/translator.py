import hashlib
import queue
import re
import threading
import time
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import runtime_events, translation_quality
from modules.pipeline_events import sentence_incomplete, sentence_metadata, sentence_text
from modules.prompt_evolver import PromptEvolver
from modules.db import _get_db
from modules.translation_prompts import (
    _BASE_PROMPT,
    _QWEN_PROMPT,
    _is_qwen_model,
    get_translation_profile,
)
from modules.translation_engines import (
    TranslationEngine,
    _build_engine_chain,
    get_last_engine_api_diagnostics,
    get_last_engine_diagnostics,
    get_last_token_usage,
    reset_last_token_usage,
)
from modules.translation_runtime import (
    FallbackState,
    active_engine,
    call_with_fallback,
)
from modules.translation_memory import MemoryLookup, TranslationMemory
from modules.translation_policy import TranslationPolicy

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_EVERY = 10   # after this many fallback calls, probe engines[0] once
_FALLBACK_THRESHOLD = 3      # consecutive primary failures before hard-switching to fallback
_TRANSLATION_WORKERS = 2
_MAX_PENDING_TRANSLATIONS = 4
_TRANSLATION_LOOP_POLL_SEC = 0.05
_API_EVENT_DEFAULTS = {
    "api_attempt_count": 0,
    "api_timeout_count": 0,
    "api_total_wall_ms": None,
    "api_final_attempt_ms": None,
    "api_first_attempt_ms": None,
    "api_retry_attempt_ms": None,
    "retry_sleep_ms": 0.0,
    "timeout_config_ms": None,
    "api_attempt_timeout_ms": None,
    "api_attempt_index": 0,
    "api_inflight_count_at_start": None,
    "source_text_char_count": None,
    "prompt_char_count": None,
    "request_body_char_count": None,
    "message_count": None,
    "context_item_count": None,
    "api_error_type": None,
    "api_error_message_class": None,
}
_CACHE_HIT_STATUSES = {"memory_hit", "db_hit"}

_HANGUL_RATIO_THRESHOLD = 0.50  # reject result if >50 % of chars are Hangul syllables
_DEPENDENCY_MARKERS = (
    "그러니까",
    "그런데",
    "그러면",
    "그러네",
    "그렇지",
    "그래서",
    "근데",
    "아니",
    "맞아",
    "그게",
    "그럼",
    "그리고",
)
_DEPENDENCY_MARKER_BOUNDARY_RE = re.compile(r"^[\s\.,!?~…。？！,，、:;；]|$")


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

_SOURCE_AWARE_TARGET_REPLACEMENTS = (
    (
        ("오버쿡드 2", "오마쿡스"),
        (
            ("Oma-kooks 立刻投！投！", "《胡鬧廚房2》"),
            ("Oma-kooks", "《胡鬧廚房2》"),
            ("Overcooked! 2", "《胡鬧廚房2》"),
            ("Overcooked 2", "《胡鬧廚房2》"),
        ),
    ),
    (
        ("어금니",),
        (("牙齦", "臼齒"), ("牙龈", "臼齒")),
    ),
    (
        ("짬밥순", "짬밥 순"),
        (("餃子順序", "資歷順"), ("年資順序", "資歷順"), ("飯順序", "資歷順")),
    ),
    (
        ("땡글즈",),
        (
            ("Tanggulz Plus怡潔", "땡글즈 Plus 이제"),
            ("Tanggulz", "땡글즈"),
            ("噹噹茲", "땡글즈"),
            ("噹噹們", "땡글즈"),
        ),
    ),
    (
        ("겟머츠",),
        (("GetMuts", "겟머츠"),),
    ),
    (
        ("띠빵뽕",),
        (("叮叮糖餅", "띠빵뽕"), ("叮糖餅", "띠빵뽕")),
    ),
    (
        ("신호등즈",),
        (("信號燈們", "信號燈즈"),),
    ),
    (
        ("선배", "생빠이", "샌바이", "센빠이"),
        (("生趴伊", "前輩"), ("森拜", "前輩")),
    ),
    (
        ("메이플", "메이플스토리", "Maple"),
        (("仙境傳說", "楓之谷"), ("MapleStory", "楓之谷"), ("Maple", "楓之谷")),
    ),
    (
        ("프린세스 메이커",),
        (("公主製造", "美少女夢工場"), ("Princess Maker", "美少女夢工場")),
    ),
    (
        ("스타크래프트", "스타컬프트"),
        (("StarCraft", "星海爭霸"), ("星際爭霸", "星海爭霸")),
    ),
    (
        ("피맛",),
        (("酒味", "血味"), ("皮馬特", "血味"), ("血的味道", "血味")),
    ),
    (
        ("창나", "창이 나"),
        (("開戰", "出事"), ("開打", "出事")),
    ),
    (
        ("다 죽여버릴 것", "죽여버릴 것"),
        (("讓人想死", "殺氣很重"), ("想死了", "殺氣很重")),
    ),
    (
        ("띵띵이",),
        (
            ("TINGGYUL", "띵띵이"),
            ("TingGyul", "띵띵이"),
            ("Tinggyul", "띵띵이"),
            ("Singgyul", "띵띵이"),
        ),
    ),
    (("하데스", "하덱스"), (("哈迪斯", "HADES"), ("哈德克斯", "HADES"))),
    (("마가 뜨", "마가뜨"), (("瑪加特", "冷場"), ("馬嘎", "冷場"), ("魔嘎", "冷場"))),
    (("붕 뜨",), (("飄起來的時間", "空掉的時間"), ("浮起來的時間", "空掉的時間"))),
    (("개복치",), (("鯛魚燒", "玻璃心"), ("翻車魚風格", "玻璃心風格"))),
    (("끼윤",), (("끼윤", "Kkiyun"),)),
    (("예난",), (("예난", "Yenan"), ("藝蘭", "Yenan"))),
    (("히나",), (("希娜", "Hina"),)),
    (("철구",), (("哲求", "Chulgu"), ("鐵球", "Chulgu"))),
    (("신빨",), (("更懂鞋", "神力更強"), ("鞋比較好", "神力比較強"))),
    (
        ("만신",),
        (
            ("幾乎都滿了，滿了", "幾乎是大神巫"),
            ("都滿了，滿了", "簡直是大神巫"),
            ("滿了，滿了", "大神巫，大神巫"),
        ),
    ),
    (
        ("다이저고 데스", "다이저 오브 데스", "다이죠부 데스", "다이조부 데스"),
        (
            ("死亡之舞", "大丈夫です"),
            ("Daisuki desu", "大丈夫です"),
            ("大好きです", "大丈夫です"),
        ),
    ),
)

_SHARED_NAME_SCOPE = "__shared__"
_STELLIVE_HINA_PROFILE_ID = "stellive_hina"
_HADES_PROFILE_ID = "hades_chxxnnx"
_MWMEU_PROFILE_ID = "mwmeu"

_SOURCE_NORM_SHARED: dict[str, str] = {}
_SOURCE_NORM_BY_PROFILE: dict[str, dict[str, str]] = {
    _STELLIVE_HINA_PROFILE_ID: {
        "히나유키 히나": "시라유키 히나",
        "해동이": "해둥이",
        "해동아": "해둥아",
        "일기생": "1기생",
        "투리버스 메들린": "투니버스 메들리",
    },
    _HADES_PROFILE_ID: {
        "服주": "섭주",
        "김띵귤": "띵귤",
        "팅귤": "띵귤",
        "틴귤": "띵귤",
        "김챗나": "챈나",
        "김챔나": "챈나",
        "챗나": "챈나",
        "챔나": "챈나",
        "채엔나": "챈나",
        "차엔나": "챈나",
        "주먹 언니": "솜펀치 언니",
        "주먹이": "솜펀치",
        "주먹아": "솜펀치",
        "큐마": "키마",
        # 채나-family → 챈나-family (longer forms first to avoid bare-substring overlap)
        "천사채나": "천사챈나",
        "채나룬": "챈나룬",
        "채나롱": "챈나롱",
        "채나로": "챈나로",
        "채나님": "챈나님",
        "채나야": "챈나야",
        "채나": "챈나",
    },
    _MWMEU_PROFILE_ID: {
        "오마쿡스 바로 투": "오버쿡드 2",
        "오마쿡스 바루 투": "오버쿡드 2",
        "오마쿡스 투": "오버쿡드 2",
        "오버쿡스 투": "오버쿡드 2",
        "오버쿡드 투": "오버쿡드 2",
        "오마쿡스": "오버쿡드",
        "오버쿡스": "오버쿡드",
        "플러스 인제": "플러스 이제",
        "생빠이": "선배",
        "샌바이": "선배",
        "센빠이": "선배",
        "토화기": "소화기",
        "소변관": "소방관",
        "가나디아": "강아지",
        "이변이": "이비",
        "이츠 언니": "리츠 언니",
        "이츠가": "리츠가",
        "이츠랑": "리츠랑",
        "이츠는": "리츠는",
        "릿츠": "리츠",
        "소아 언니": "수아 언니",
        "소아가": "수아가",
        "초운이": "초은이",
        "초운": "초은",
        "조은이": "초은이",
        "조은아": "초은아",
        "조은": "초은",
        "초원이랑": "초은이랑",
        "초원이": "초은이",
        "엔즈": "웬즈",
        "왠즈": "웬즈",
        "왠지들": "웬즈들",
        "지안 언니": "지한 언니",
        "지안언니": "지한언니",
        "지안이": "지한이",
        "시에가파크": "치이카와파크",
        "치카와파크": "치이카와파크",
        "치에이카와": "치이카와",
        "치에카와": "치이카와",
        "시이카와": "치이카와",
        "시가와": "치이카와",
        "지휘카와": "치이카와",
        "치유카": "치이카와",
        "치이칸": "치이카와",
        "치카와": "치이카와",
        "하치와래": "하치와레",
        "하츠와 아래": "하치와레",
        "하츠와": "하치와레",
    },
}

_PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS: dict[
    str, tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], ...]
] = {
    _STELLIVE_HINA_PROFILE_ID: (
        (
            ("해둥이", "해둥아", "해둥", "해동이", "해동아"),
            (
                ("海洞啊", "해둥아"),
                ("海洞們", "해둥이們"),
                ("海洞们", "해둥이們"),
                ("海洞", "해둥이"),
            ),
        ),
        (
            ("유니", "아야츠노 유니"),
            (("優妮", "Yuni"), ("優尼", "Yuni"), ("尤尼", "Yuni")),
        ),
        (
            ("1기생",),
            (("日記生", "1期生"),),
        ),
        (
            ("시라유키 히나", "히나유키 히나"),
            (
                ("希拉尤基·Hina", "Shirayuki Hina"),
                ("希拉尤基 Hina", "Shirayuki Hina"),
            ),
        ),
        (
            ("투니버스 메들리", "투리버스 메들린"),
            (
                ("Touriverus Madeline", "투니버스 메들리"),
                ("Touriverse Madeline", "투니버스 메들리"),
                ("Touriverus", "투니버스"),
                ("Touriverse", "투니버스"),
            ),
        ),
    ),
}

_KOREAN_NAME_SUFFIXES = frozenset(
    (
        "이에요",
        "입니다",
        "에게",
        "한테",
        "이랑",
        "하고",
        "예요",
        "이다",
        "누나",
        "언니",
        "오빠",
        "님",
        "씨",
        "형",
        "가",
        "이",
        "은",
        "는",
        "을",
        "를",
        "의",
        "도",
        "만",
        "에",
        "께",
        "랑",
        "과",
        "와",
        "야",
        "아",
        "들",
        "보다",
    )
)


@dataclass(frozen=True)
class _NameRenderingRule:
    scope: str
    source_aliases: tuple[str, ...]
    wrong_forms: tuple[str, ...]
    canonical: str


_NAME_RENDERING_RULES = (
    _NameRenderingRule(
        _STELLIVE_HINA_PROFILE_ID,
        ("시라유키 히나", "히나유키 히나"),
        ("시라유키 히나", "히나유키 히나", "希拉尤基·Hina", "希拉尤基 Hina"),
        "Shirayuki Hina",
    ),
    _NameRenderingRule(
        _STELLIVE_HINA_PROFILE_ID,
        ("아야츠노 유니",),
        ("아야츠노 유니", "Ayatsuno Yuni"),
        "Ayatsuno Yuni",
    ),
    _NameRenderingRule(
        _STELLIVE_HINA_PROFILE_ID,
        ("유니",),
        ("유니", "優妮", "優尼", "尤尼"),
        "Yuni",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("챈나", "김챗나", "김챔나", "챗나", "챔나", "Chaenna", "CHXXNNX", "Chxxnnx"),
        (
            "챈나",
            "Chaenna",
            "CHXXNNX",
            "-chan",
            "-Chan",
            "－chan",
            "－Chan",
            "–chan",
            "–Chan",
            "—chan",
            "—Chan",
            "金chat",
            "金Chat",
            "金챗나",
            "金챔나",
        ),
        "Chxxnnx",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("솜주먹", "솜펀치", "주먹이", "주먹아", "주먹 언니"),
        ("솜주먹", "솜펀치", "桑拳頭", "拳頭", "棉拳", "Som Punch"),
        "Sompunch",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("띵귤", "김띵귤", "싱귤"),
        (
            "띵귤",
            "싱귤",
            "TINGGYUL",
            "TingGyul",
            "Tinggyul",
            "金叮菊",
            "金丁橘",
            "叮菊",
            "丁橘",
        ),
        "Singgyul",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("김봉준", "봉준"),
        ("김봉준", "봉준", "Bongjun", "奉俊", "奉主"),
        "Kim Bongjun",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("성태",),
        ("성태", "Sungtae老師", "Sungtae哥", "Sungtae", "成泰", "狀態哥"),
        "KimSungtae",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("키마", "큐마"),
        ("키마", "큐마", "Kima", "基馬"),
        "Kyma",
    ),
    _NameRenderingRule(
        _SHARED_NAME_SCOPE,
        ("고세구",),
        ("高世久",),
        "Gosegu",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("지한", "지한이"),
        ("지한", "지안", "志安", "智漢", "志漢", "Z-Han", "Zhan", "ZHAN"),
        "지한",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("이비",),
        ("이비", "이변이", "伊比", "伊變", "李比", "Ivi", "IVI"),
        "이비",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("수아",),
        ("수아", "소아", "水亞", "數亞", "素亞", "Sua", "SuA", "SUA"),
        "수아",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("리츠",),
        ("리츠", "이츠", "릿츠", "利茨", "米茨", "Rits", "Ritz", "リツ"),
        "리츠",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("초은", "초은이"),
        ("초은", "초운", "조은", "초원", "初恩", "初雲", "Choeun", "Cho-eun"),
        "초은",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("웬즈", "웬즈들", "WENs"),
        ("웬즈", "왠즈", "왠지", "엔즈", "wenz", "Wenz", "WENZ", "溫斯"),
        "WENs",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("치이카와", "치이카와파크"),
        ("치이카와", "치이카와파크", "치카와", "千川", "奇卡瓦", "奇伊卡", "芝伊卡", "千代田", "市川", "Pekawa"),
        "Chiikawa",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("하치와레",),
        ("하치와레", "하치와래", "哈奇瓦雷", "哈奇瓦", "哈茨", "黑豆"),
        "Hachiware",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("모몽가",),
        ("모몽가", "摩蒙加", "蒙蒙加", "毛毛蟲"),
        "Momonga",
    ),
    _NameRenderingRule(
        _MWMEU_PROFILE_ID,
        ("우사기",),
        ("우사기", "烏薩奇", "兔子"),
        "Usagi",
    ),
)


def _looks_like_meta_garbage_output(result: str) -> bool:
    normalized = result.strip()
    if not normalized:
        return False
    if "STT" in normalized.upper() and any(marker in normalized for marker in _META_GARBAGE_MARKERS):
        return True
    if normalized.startswith(("(", "（", "[", "【")) and any(
        marker in normalized for marker in _META_GARBAGE_MARKERS
    ):
        return True
    return False


def _is_hangul_syllable(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _is_name_suffix_boundary(char: str) -> bool:
    return char.isspace() or not char.isalnum()


def _source_alias_matches_at(source: str, alias: str, start: int) -> bool:
    if start > 0 and _is_hangul_syllable(source[start - 1]):
        return False

    end = start + len(alias)
    if end >= len(source):
        return True

    next_char = source[end]
    if not _is_hangul_syllable(next_char):
        return True

    suffix_end = end
    while suffix_end < len(source) and _is_hangul_syllable(source[suffix_end]):
        suffix_end += 1

    suffix = source[end:suffix_end]
    if suffix not in _KOREAN_NAME_SUFFIXES:
        return False

    return suffix_end >= len(source) or _is_name_suffix_boundary(source[suffix_end])


def _source_has_name_alias(source: str, aliases: tuple[str, ...]) -> bool:
    for alias in aliases:
        if not alias:
            continue
        start = source.find(alias)
        while start >= 0:
            if _source_alias_matches_at(source, alias, start):
                return True
            start = source.find(alias, start + 1)
    return False


def _name_rendering_rule_enabled(rule: _NameRenderingRule) -> bool:
    if rule.scope == _SHARED_NAME_SCOPE:
        return True
    return bool(cfg.translation.use_profile) and cfg.active_streamer_profile == rule.scope


# Per-worker record of source-normalization / target-correction rules that
# actually fired on the current translation, so the runtime event can show
# whether "海洞 -> 해둥이"-style rescues are routine or rarely needed anymore.
_LAST_CORRECTIONS = threading.local()


def reset_corrections() -> None:
    _LAST_CORRECTIONS.value = []


def _record_correction(stage: str, rule: str, before: str, after: str) -> None:
    bucket = getattr(_LAST_CORRECTIONS, "value", None)
    if not isinstance(bucket, list):
        bucket = []
        _LAST_CORRECTIONS.value = bucket
    bucket.append({"stage": stage, "rule": rule, "before": before, "after": after})


def get_corrections() -> list[dict]:
    value = getattr(_LAST_CORRECTIONS, "value", None)
    return list(value) if isinstance(value, list) else []


def _replace_recording(text: str, wrong: str, right: str, *, stage: str, rule_id: str) -> str:
    """Apply text.replace(wrong, right), recording the rule iff it changed text."""
    if wrong and wrong in text:
        new = text.replace(wrong, right)
        if new != text:
            _record_correction(stage, rule_id, wrong, right)
            return new
    return text


def _replace_wrong_name_forms(result: str, rule: _NameRenderingRule) -> str:
    if not rule.wrong_forms:
        return result

    alternatives = sorted({rule.canonical, *rule.wrong_forms}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(alternative) for alternative in alternatives))
    corrected = pattern.sub(rule.canonical, result)
    if corrected != result:
        present = "|".join(form for form in rule.wrong_forms if form in result)
        _record_correction("name_render", f"name:{rule.canonical}", present, rule.canonical)
    return corrected


def _normalize_source_before_matching(text: str) -> str:
    """Replace known unambiguous STT noise forms with their canonical source alias.

    Runs before slang lookup, cache lookup, LLM call, and source-aware corrections.
    Operates on prepared text only; raw_text stored in TranslationOutcome is untouched.
    Profile-gated: normalization only applies when the matching profile is active.
    """
    norm: dict[str, str] = dict(_SOURCE_NORM_SHARED)
    profile_id = cfg.active_streamer_profile
    if profile_id and bool(cfg.translation.use_profile):
        norm.update(_SOURCE_NORM_BY_PROFILE.get(profile_id, {}))
    for noisy, canonical in sorted(norm.items(), key=lambda item: len(item[0]), reverse=True):
        text = _replace_recording(
            text, noisy, canonical, stage="source_norm", rule_id=f"{noisy}->{canonical}"
        )
    return text


def _apply_source_aware_corrections(source: str, result: str) -> str:
    corrected = result
    for source_terms, replacements in _SOURCE_AWARE_TARGET_REPLACEMENTS:
        if not any(term in source for term in source_terms):
            continue
        for wrong, right in replacements:
            corrected = _replace_recording(
                corrected, wrong, right, stage="target_correction", rule_id=f"{wrong}->{right}"
            )

    if cfg.translation.use_profile:
        profile_replacements = _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS.get(cfg.active_streamer_profile, ())
        for source_terms, replacements in profile_replacements:
            if not any(term in source for term in source_terms):
                continue
            for wrong, right in replacements:
                corrected = _replace_recording(
                    corrected, wrong, right,
                    stage="target_correction", rule_id=f"profile:{wrong}->{right}",
                )

    for rule in _NAME_RENDERING_RULES:
        if not _name_rendering_rule_enabled(rule):
            continue
        if not _source_has_name_alias(source, rule.source_aliases):
            continue
        corrected = _replace_wrong_name_forms(corrected, rule)

    if "무당" in source and "신발" in source:
        corrected = _replace_recording(
            corrected, "更懂鞋", "神力更強", stage="target_correction", rule_id="mudang_shoes"
        )
        corrected = _replace_recording(
            corrected, "更懂鞋子", "神力更強", stage="target_correction", rule_id="mudang_shoes"
        )

    return corrected


def _dependency_marker(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    for marker in _DEPENDENCY_MARKERS:
        if not stripped.startswith(marker):
            continue
        suffix = stripped[len(marker):]
        if _DEPENDENCY_MARKER_BOUNDARY_RE.match(suffix):
            return marker
    return ""


def _looks_untranslated(result: str, source: str) -> bool:
    if result == source:
        return True
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


def _write_history(ko: str, zh: str) -> None:
    path = _LOG_DIR / f"translations_{datetime.now().strftime('%Y%m%d')}.txt"
    ts = datetime.now().strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {ko}\n        → {zh}\n")


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TranslationOutcome:
    source_text: str
    target_text: str | None
    status: str
    result_source: str
    cache_status: str
    incomplete: bool
    engine: str = ""
    model: str = ""
    prompt_version: str = ""
    filter_reason: str = ""

    def as_event_fields(self, latency_ms: float, metadata: dict) -> dict:
        return {
            "source_text": self.source_text,
            "target_text": self.target_text,
            "status": self.status,
            "result_source": self.result_source,
            "cache_status": self.cache_status,
            "incomplete": self.incomplete,
            "engine": self.engine,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "filter_reason": self.filter_reason,
            "latency_ms": round(latency_ms, 2),
            **metadata,
            **translation_quality(self.source_text, self.target_text),
        }


@dataclass
class _TranslatorSharedState:
    evolver: PromptEvolver
    memory: TranslationMemory
    policy: TranslationPolicy
    fallback: FallbackState
    lock: object


def _new_translation_memory() -> TranslationMemory:
    recent_window = max(getattr(cfg.translation, 'context_window', 0) or 0, 30)
    return TranslationMemory(
        recent_window=recent_window,
        max_cache_size=_CACHE_MAX_SIZE,
        db_factory=_get_db,
        history_writer=_write_history,
    )


def _new_translation_policy() -> TranslationPolicy:
    return TranslationPolicy(
        slang=cfg.translation.slang,
        min_translate_chars=_MIN_TRANSLATE_CHARS,
        max_translate_chars=cfg.translation.max_translate_chars,
    )


def _new_translator_shared_state() -> _TranslatorSharedState:
    return _TranslatorSharedState(
        evolver=PromptEvolver(),
        memory=_new_translation_memory(),
        policy=_new_translation_policy(),
        fallback=FallbackState(),
        lock=threading.RLock(),
    )


def _outcome_used_api(outcome: TranslationOutcome) -> bool:
    if outcome.result_source == "api":
        return True
    if outcome.status == "failed" and outcome.result_source == "none":
        return True
    if outcome.result_source == "post_policy" and outcome.cache_status not in _CACHE_HIT_STATUSES:
        return True
    return False


def _api_event_fields(
    outcome: TranslationOutcome,
    diagnostics: dict[str, int | float | str | None],
) -> dict:
    fields = dict(_API_EVENT_DEFAULTS)
    engine = str(diagnostics.get("engine") or "")
    if not engine or engine != outcome.engine or not _outcome_used_api(outcome):
        return fields
    if int(diagnostics.get("api_attempt_count") or 0) <= 0:
        return fields
    for key in fields:
        fields[key] = diagnostics.get(key, fields[key])
    return fields


def _retry_diagnostics_apply(outcome: TranslationOutcome, diagnostics: dict[str, int | str]) -> bool:
    engine = str(diagnostics.get("engine") or "")
    return bool(engine and engine == outcome.engine and _outcome_used_api(outcome))


@dataclass(frozen=True)
class _CompletedTranslation:
    seq: int
    outcome: TranslationOutcome
    elapsed: float
    metadata: dict
    submitted_at: float
    started_at: float
    completed_at: float
    worker_id: str
    retry_count: int
    retry_reason: str
    api_event_fields: dict


class Translator:
    def __init__(self, shared_state: _TranslatorSharedState | None = None):
        self._shared_state = shared_state or _new_translator_shared_state()
        self._state_lock = self._shared_state.lock
        self._fallback_state_obj = self._shared_state.fallback
        self._evolver = self._shared_state.evolver
        self._engines: list[TranslationEngine] = _build_engine_chain()
        self._memory = self._shared_state.memory
        self._policy = self._shared_state.policy
        self._last_input: str = ""

    def _state_guard(self):
        return getattr(self, "_state_lock", None) or nullcontext()

    def _fallback_state(self) -> FallbackState:
        state = getattr(self, "_fallback_state_obj", None)
        if state is None:
            state = FallbackState()
            self._fallback_state_obj = state
        return state

    @property
    def _active_idx(self) -> int:
        return self._fallback_state().active_idx

    @_active_idx.setter
    def _active_idx(self, value: int) -> None:
        self._fallback_state().active_idx = value

    @property
    def _probe_counter(self) -> int:
        return self._fallback_state().probe_counter

    @_probe_counter.setter
    def _probe_counter(self, value: int) -> None:
        self._fallback_state().probe_counter = value

    @property
    def _consecutive_primary_failures(self) -> int:
        return self._fallback_state().consecutive_primary_failures

    @_consecutive_primary_failures.setter
    def _consecutive_primary_failures(self, value: int) -> None:
        self._fallback_state().consecutive_primary_failures = value

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        return self.translate_event(text, incomplete).target_text

    def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
        raw_text = (text or "").strip()
        policy = self._policy_state()
        filter_reason = policy.rejection_reason(raw_text)
        text = self._prepare_input(text)
        if text is None:
            return TranslationOutcome(
                source_text=raw_text,
                target_text=None,
                status="filtered",
                result_source="policy",
                cache_status="skipped",
                incomplete=incomplete,
                filter_reason=filter_reason or policy._last_sanitize_rejection or "unknown",
            )

        text = _normalize_source_before_matching(text)

        if slang_result := self._translate_slang(text, incomplete):
            return TranslationOutcome(
                source_text=raw_text,
                target_text=slang_result,
                status="success",
                result_source="slang",
                cache_status="skipped",
                incomplete=incomplete,
            )

        # 根据当前模型选择对应的 prompt
        system_prompt = self._build_system_prompt()
        prompt_ver = self._prompt_version(system_prompt)
        self._log_prompt_mode_once()

        lookup = self._lookup_existing_translation_event(text, incomplete, prompt_ver)
        engine = self._active_engine()
        if lookup.result:
            target_text = _apply_source_aware_corrections(text, lookup.result)
            if _looks_like_meta_garbage_output(target_text):
                return TranslationOutcome(
                    source_text=raw_text,
                    target_text=None,
                    status="filtered",
                    result_source="post_policy",
                    cache_status=lookup.source,
                    incomplete=incomplete,
                    filter_reason="meta_garbage_output",
                    engine=engine.engine_name if engine else "",
                    model=engine.model_name if engine else "",
                    prompt_version=prompt_ver,
                )
            return TranslationOutcome(
                source_text=raw_text,
                target_text=target_text,
                status="success",
                result_source=lookup.source,
                cache_status=lookup.source,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_ver,
            )

        with self._state_guard():
            history = self._memory_state().context()
        result = self._call_with_fallback(text, system_prompt, incomplete, history)
        engine = self._active_engine()
        if result:
            result = _apply_source_aware_corrections(text, result)
            if _looks_like_meta_garbage_output(result):
                log.debug("Filtering meta garbage translation output: %.40s -> %.40s", text, result)
                return TranslationOutcome(
                    source_text=raw_text,
                    target_text=None,
                    status="filtered",
                    result_source="post_policy",
                    cache_status=lookup.source,
                    incomplete=incomplete,
                    engine=engine.engine_name if engine else "",
                    model=engine.model_name if engine else "",
                    prompt_version=prompt_ver,
                    filter_reason="meta_garbage_output",
                )
            self._record_success(text, result, incomplete, prompt_ver)
            return TranslationOutcome(
                source_text=raw_text,
                target_text=result,
                status="success",
                result_source="api",
                cache_status=lookup.source,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_ver,
            )
        else:
            # API failure — allow next identical input to retry rather than staying suppressed
            self._policy_state().reset_last_input()
            self._last_input = ""
        return TranslationOutcome(
            source_text=raw_text,
            target_text=None,
            status="failed",
            result_source="none",
            cache_status=lookup.source,
            incomplete=incomplete,
            engine=engine.engine_name if engine else "",
            model=engine.model_name if engine else "",
            prompt_version=prompt_ver,
        )

    def _prepare_input(self, text: str) -> str | None:
        with self._state_guard():
            prepared = self._policy_state().prepare_input(text)
            self._last_input = self._policy_state().last_input
        return prepared

    def _policy_state(self) -> TranslationPolicy:
        return self._policy

    def _memory_state(self) -> TranslationMemory:
        return self._memory

    def _translate_slang(self, text: str, incomplete: bool) -> str | None:
        slang_result = self._policy_state().slang_result(text)
        if not slang_result:
            return None

        log.debug("Slang hit: %s → %s", text, slang_result)
        with self._state_guard():
            self._evolver.record(text, slang_result)
            self._memory_state().record_direct(text, slang_result, incomplete)
        return slang_result

    def _lookup_existing_translation_event(self, text: str, incomplete: bool,
                                           prompt_ver: str) -> MemoryLookup:
        with self._state_guard():
            lookup = self._memory_state().lookup_existing_event(
                text,
                incomplete,
                prompt_ver,
                self._active_engine(),
            )
        if lookup.result:
            log.debug("Cache hit: %s", text[:20])
        return lookup

    def _record_success(self, text: str, result: str, incomplete: bool,
                        prompt_ver: str) -> None:
        with self._state_guard():
            self._evolver.record(text, result)
            self._memory_state().record_success(
                text,
                result,
                incomplete,
                prompt_ver,
                self._active_engine(),
            )

    def _active_engine(self) -> TranslationEngine | None:
        return active_engine(self._engines, self._active_idx)

    def _log_prompt_mode_once(self) -> None:
        if _is_qwen_model() and not hasattr(self, '_qwen_log_once'):
            log.info("Using Qwen-optimized system prompt (shorter, more direct)")
            self._qwen_log_once = True

    def _build_system_prompt(self) -> str:
        is_qwen = _is_qwen_model()
        base_prompt = _QWEN_PROMPT if is_qwen else _BASE_PROMPT
        with self._state_guard():
            system_prompt = self._evolver.build_system_prompt(base_prompt)

        if not cfg.translation.use_profile:
            return system_prompt

        streamer_profile = get_translation_profile(cfg.active_streamer_profile, qwen=is_qwen)
        if streamer_profile:
            system_prompt += "\n\n" + streamer_profile
            log.debug("Appended streamer profile: %s", cfg.active_streamer_profile)

        return system_prompt

    @staticmethod
    def _prompt_version(system_prompt: str) -> str:
        return hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    def _call_with_fallback(self, text: str, system_prompt: str, incomplete: bool,
                            history: list[tuple[str, str]] | None = None) -> str | None:
        fallback_state = self._fallback_state()
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            state = fallback_state
        else:
            with lock:
                state = FallbackState(
                    fallback_state.active_idx,
                    fallback_state.probe_counter,
                    fallback_state.consecutive_primary_failures,
                )
        result = call_with_fallback(
            self._engines,
            state,
            text,
            system_prompt,
            incomplete,
            history,
            _FALLBACK_PROBE_EVERY,
            _FALLBACK_THRESHOLD,
            _looks_untranslated,
            log,
        )
        if lock is not None:
            with lock:
                fallback_state.active_idx = state.active_idx
                fallback_state.probe_counter = state.probe_counter
                fallback_state.consecutive_primary_failures = state.consecutive_primary_failures
        return result

    def _get_prompt_version_hash(self) -> str:
        return self._prompt_version(self._build_system_prompt())


_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window


def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        shared_state = _new_translator_shared_state()
        worker_state = threading.local()
        executor = ThreadPoolExecutor(
            max_workers=_TRANSLATION_WORKERS,
            thread_name_prefix="TranslationWorker",
        )
        pending: dict[int, Future[_CompletedTranslation]] = {}
        completed: dict[int, _CompletedTranslation] = {}
        next_seq = 0
        next_emit_seq = 0
        last_result = ""
        last_result_time = 0.0

        def translate_item(seq: int, item, submitted_at: float) -> _CompletedTranslation:
            text = sentence_text(item)
            incomplete = sentence_incomplete(item)
            metadata = sentence_metadata(item).copy()
            marker = _dependency_marker(text)
            metadata.update(
                {
                    "sequence_id": seq,
                    "starts_with_dependency_marker": bool(marker),
                    "dependency_marker": marker,
                    "profile_id": cfg.active_streamer_profile,
                    "profile_applied": bool(getattr(cfg.translation, "use_profile", False)),
                }
            )
            started = time.monotonic()
            worker_id = threading.current_thread().name
            # Reset before translating so cache hits / failures (which never reach an
            # engine) don't inherit the previous call's token usage / corrections.
            reset_last_token_usage()
            reset_corrections()
            try:
                worker_translator = getattr(worker_state, "translator", None)
                if worker_translator is None:
                    try:
                        worker_translator = Translator(shared_state=shared_state)
                    except TypeError:
                        worker_translator = Translator()
                    worker_state.translator = worker_translator
                outcome = worker_translator.translate_event(text, incomplete)
            except Exception:
                log.exception("Translation worker failed for: %.40s", text)
                outcome = TranslationOutcome(
                    source_text=(text or "").strip(),
                    target_text=None,
                    status="failed",
                    result_source="none",
                    cache_status="skipped",
                    incomplete=incomplete,
                )
            completed_at = time.monotonic()
            elapsed = completed_at - started
            diagnostics = get_last_engine_diagnostics()
            api_diagnostics = get_last_engine_api_diagnostics()
            for usage_key, usage_value in get_last_token_usage().items():
                if usage_value is not None:
                    metadata[f"token_{usage_key}"] = usage_value
            corrections = get_corrections()
            if corrections:
                metadata["corrections"] = corrections
                metadata["correction_count"] = len(corrections)
            retry_count = 0
            retry_reason = ""
            if _retry_diagnostics_apply(outcome, diagnostics):
                retry_count = int(diagnostics.get("retry_count") or 0)
                retry_reason = str(diagnostics.get("retry_reason") or "")
            api_event_fields = _api_event_fields(outcome, api_diagnostics)
            return _CompletedTranslation(
                seq,
                outcome,
                elapsed,
                metadata,
                submitted_at,
                started,
                completed_at,
                worker_id,
                retry_count,
                retry_reason,
                api_event_fields,
            )

        def collect_finished() -> None:
            for seq, future in list(pending.items()):
                if not future.done():
                    continue
                pending.pop(seq)
                try:
                    completed[seq] = future.result()
                except Exception:
                    log.exception("Translation future failed")

        def emit_completed(item: _CompletedTranslation) -> None:
            nonlocal last_result, last_result_time
            outcome = item.outcome
            elapsed = item.elapsed
            emitted_at = time.monotonic()
            event_metadata = item.metadata.copy()
            event_metadata.update(
                {
                    "engine_latency_ms": round(elapsed * 1000, 2),
                    "queue_wait_ms": round(max(0.0, item.started_at - item.submitted_at) * 1000, 2),
                    "output_delay_ms": round(max(0.0, emitted_at - item.submitted_at) * 1000, 2),
                    "predecessor_stall_ms": round(max(0.0, emitted_at - item.completed_at) * 1000, 2),
                    "translation_worker_id": item.worker_id,
                    "retry_count": item.retry_count,
                    "retry_reason": item.retry_reason,
                    **item.api_event_fields,
                }
            )
            metrics.observe_latency("translation", elapsed)
            event_fields = outcome.as_event_fields(elapsed * 1000, event_metadata)
            result = outcome.target_text
            if result:
                metrics.increment("translation.success")
                # Surface low-quality output in the 60 s metrics summary so a
                # degrading stretch is visible without scraping the JSONL.
                severity = event_fields.get("quality_severity")
                if severity in ("bad", "warn"):
                    metrics.increment(f"translation.quality.{severity}")
                now = time.monotonic()
                if result == last_result and (now - last_result_time) < _DEDUP_SUBTITLE_SEC:
                    log.debug("Suppressing duplicate subtitle: %s", result[:30])
                    runtime_events.emit(
                        "translation",
                        **event_fields,
                        subtitle_emitted=False,
                        subtitle_suppressed_reason="duplicate",
                    )
                    return
                last_result = result
                last_result_time = now
                put_latest(subtitle_queue, result, log, "subtitle_queue")
                runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=True,
                    subtitle_suppressed_reason="",
                )
            else:
                metrics.increment("translation.empty")
                runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=False,
                    subtitle_suppressed_reason="",
                )
            metrics.log_summary_if_due()

        try:
            while not stop_event.is_set():
                collect_finished()
                while next_emit_seq in completed:
                    emit_completed(completed.pop(next_emit_seq))
                    next_emit_seq += 1

                if len(pending) >= _MAX_PENDING_TRANSLATIONS:
                    stop_event.wait(_TRANSLATION_LOOP_POLL_SEC)
                    continue

                has_item, item = poll_queue(
                    sentence_queue,
                    stop_event,
                    pause_event,
                    timeout=_TRANSLATION_LOOP_POLL_SEC,
                )
                if has_item:
                    submitted_at = time.monotonic()
                    pending[next_seq] = executor.submit(translate_item, next_seq, item, submitted_at)
                    next_seq += 1
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            log.info("Translator stopped")

    return start_daemon_thread("Translator", run)


if __name__ == "__main__":
    translator = Translator()
    tests = [
        ("안녕하세요, 오늘 방송에 오신 걸 환영해요!", False),
        ("진짜 대박이다 ㅋㅋㅋ", False),
        ("지금 게임 하고", True),
    ]
    for text, incomplete in tests:
        result = translator.translate(text, incomplete)
        print(f"{text!r} → {result!r}")
