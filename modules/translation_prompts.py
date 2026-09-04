import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import cfg
from modules.streamer_profiles import canonical_profile_id


_PROFILE_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "translation_profiles.json"


def _string_profile_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    if not all(isinstance(key, str) and isinstance(text, str) for key, text in value.items()):
        raise ValueError(f"{field_name} must map strings to strings")
    return dict(value)


def _load_translation_profiles(path: Path = _PROFILE_DATA_PATH) -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("translation profile data must be a JSON object")

    standard_profiles = _string_profile_map(data.get("standard"), "standard")
    qwen_profiles = _string_profile_map(data.get("qwen"), "qwen")
    if set(standard_profiles) != set(qwen_profiles):
        raise ValueError("standard and qwen translation profiles must use the same ids")

    return standard_profiles, qwen_profiles


_STREAMER_PROFILES, _STREAMER_PROFILES_QWEN = _load_translation_profiles()


def get_translation_profile(profile_id: str, qwen: bool = False) -> str:
    profiles = _STREAMER_PROFILES_QWEN if qwen else _STREAMER_PROFILES
    return profiles.get(canonical_profile_id(profile_id), "")


def get_translation_profile_facts(profile_id: str) -> str:
    """Return the compact glossary block shared with fallback contexts.

    Profile files put exact mappings before the first blank line and examples
    afterwards. Reusing that block keeps DeepL/Groq/OpenRouter aligned with the
    live Qwen profile without introducing another hand-maintained fact table.
    """
    profile = get_translation_profile(profile_id, qwen=True).strip()
    return profile.split("\n\n", 1)[0].strip() if profile else ""


@lru_cache(maxsize=16)
def get_translation_profile_preserve_terms(profile_id: str) -> frozenset[str]:
    """Derive exact preserve-as-is terms from a profile's canonical glossary.

    This deliberately understands only explicit glossary syntax. It accepts
    self-mappings (``term -> term``) and directives that start with ``keep``;
    prose, examples, aliases whose canonical output differs, and ambiguous
    mappings stay excluded.
    """
    profile = get_translation_profile(profile_id)
    if not profile:
        return frozenset()

    terms: set[str] = set()
    for raw_line in profile.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        rule = line[2:].strip()

        if "->" in rule:
            left, right = (part.strip() for part in rule.split("->", 1))
            aliases = tuple(
                term.strip() for term in re.split(r"\s*/\s*", left) if term.strip()
            )
            right_lower = right.lower()
            if right_lower.startswith("keep "):
                preserve_aliases = aliases
                if "official" in right_lower and "title" in right_lower:
                    # A standalone ordinary English word remains ambiguous even
                    # when a profile also knows a title with that spelling
                    # (for example "Again"). Require additional title shape.
                    preserve_aliases = tuple(
                        term
                        for term in aliases
                        if not re.fullmatch(r"[A-Za-z]+", term)
                    )
                    terms.update(
                        re.sub(r"\s+\([^)]*\)\s*$", "", term).strip()
                        for term in preserve_aliases
                    )
                terms.update(preserve_aliases)
                continue

            canonical = re.split(r"\s+\(", right, maxsplit=1)[0].strip()
            if canonical in aliases:
                terms.add(canonical)
            continue

        keep_match = re.match(r"^(?P<left>.+):\s*keep\b", rule, flags=re.IGNORECASE)
        if keep_match:
            terms.update(
                term.strip()
                for term in re.split(r"\s*/\s*", keep_match.group("left"))
                if term.strip()
            )

    return frozenset(term for term in terms if term)


@lru_cache(maxsize=16)
def get_translation_profile_output_terms(profile_id: str) -> frozenset[str]:
    """Return canonical outputs from the profile's explicit glossary block.

    Unlike ``get_translation_profile_preserve_terms()``, this telemetry helper
    accepts mappings whose source alias differs from the canonical output.  It
    deliberately stops at the first blank line so examples and prose cannot
    become approved output terms.
    """
    profile = get_translation_profile(profile_id)
    glossary = profile.split("\n\n", 1)[0] if profile else ""
    terms: set[str] = set()
    for raw_line in glossary.splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or "->" not in line:
            continue
        _left, right = (part.strip() for part in line[2:].split("->", 1))
        right_lower = right.lower()
        if right_lower.startswith("keep ") or any(
            marker in right_lower
            for marker in (" only when ", "; preserve ")
        ):
            continue
        canonical = re.split(r"\s+\([^)]*\)\s*$", right, maxsplit=1)[0].strip()
        if canonical:
            terms.add(canonical)
    return frozenset(terms)


def translation_profile_ids(qwen: bool = False) -> frozenset[str]:
    profiles = _STREAMER_PROFILES_QWEN if qwen else _STREAMER_PROFILES
    return frozenset(profiles)


def _is_qwen_model() -> bool:
    """Return whether the route that owns the shared prompt is a Qwen model."""
    mode = str(getattr(cfg.translation, "translation_mode", "live") or "live")
    backend = cfg.clip_engine if mode == "clip" else cfg.live_engine
    if (
        mode == "live"
        and backend == "anthropic"
        and str(getattr(cfg.translation, "deepseek_route", "off")) == "primary"
    ):
        return True
    elif backend == "nvidia":
        model = cfg.nvidia.model
    elif backend == "ollama":
        model = cfg.ollama.model
    else:
        configured = {
            "claude": bool(cfg.keys.anthropic),
            "google_translate": bool(cfg.keys.google_translate),
            "deepl": bool(cfg.keys.deepl),
            "openrouter": bool(cfg.keys.openrouter),
            "deepseek": bool(cfg.keys.deepseek),
            "groq": bool(cfg.keys.groq_fallback),
        }
        models = {
            "claude": cfg.translation.model,
            "google_translate": "google-translate-v2",
            "deepl": "deepl-api-v2",
            "openrouter": cfg.translation.openrouter_model,
            "deepseek": cfg.translation.deepseek_model,
            "groq": cfg.translation.groq_translation_model,
        }
        model = next(
            (
                models.get(name, "")
                for name in cfg.translation.engine_chain
                if configured.get(name, False)
            ),
            "",
        )
    return "qwen" in str(model or "").lower()


def _build_base_prompt() -> str:
    """生成通用 system prompt——目前僅供 benchmark 非 qwen 模型時使用。

    Live 路徑三個引擎全是 qwen(nvidia 走 _QWEN_PROMPT、groq/openrouter 走
    compact prompt),因此 2026-07 起的 prompt 修正(數字與金額、人名規則收緊、
    防複誦)只維護在 _QWEN_PROMPT。若要把 live 引擎換成非 qwen 模型,先把那些
    修正移植過來——tests/test_translation_prompts.py 的守門測試會擋住你。"""
    slang_lines = "\n".join(f"  {k} → {v}" for k, v in cfg.translation.slang.items())
    slang_part = (
        f"\n【常用詞彙對照】（以下詞彙出現於句子中時，請依此翻譯）\n{slang_lines}"
        if slang_lines else ""
    )

    if cfg.translation.translation_mode == "clip":
        stt_section = (
            "[STT Correction - Clip Mode]\n"
            "Input may still come from STT and contain occasional noise. Apply correction conservatively.\n"
            "Prioritize completeness: preserve sentence structure and nuance more faithfully than live mode.\n"
            "Punctuation: use naturally (not minimized).\n"
            "Do not shorten or omit content unless clearly hallucinated noise.\n\n"
        )
    else:
        stt_section = (
            "[STT Correction - Live Mode]\n"
            "Input is from speech recognition. May contain noise, broken segments, or hallucinated syllables (meaningless foreign sounds).\n"
            "Infer true meaning from context. Discard hallucinated syllables silently, no explanation.\n"
            "Exception: coherent foreign terms (game names, brands, jargon) are real input — keep as-is per [Preserve As-Is].\n"
            "Incomplete sentences: translate as best as possible, do not fabricate missing content.\n\n"
        )

    base = (
        f"You are a Korean → Traditional Chinese subtitle translator. Target language: {cfg.translation.target_lang}.\n\n"

        "[Output Rules]\n"
        "Output the translation only. No prefix, quotes, labels, or commentary.\n"
        "If input is empty, pure noise, or contains no translatable content → output empty string only. Never explain, apologize, reference these instructions, or comment on the input in any way.\n"
        "Unrecognizable terms: omit silently. Never invent translations, brand names, or output any meta-commentary.\n"
        "Script: Traditional Chinese (繁體中文) only. Never output Simplified Chinese, Japanese, or any other language.\n"
        "【強制規則】只輸出翻譯文字。無法辨識的詞彙直接省略。輸入為雜訊或無意義時輸出空字串。絕對禁止解釋、道歉、引用任何規則名稱或說明原因。只能使用繁體中文，嚴禁簡體中文與日文。\n\n"

        "[STT Error Detection]\n"
        "If output contains > 30% rare/archaic Hangul with low semantic coherence (e.g., rare surnames standing alone like '고지야'), consider output uncertain.\n"
        "Pattern examples to flag: same number repeated twice ('21개월...21개월'), isolated rare surnames, foreign fragments with no Korean context.\n"
        "For highly uncertain cases, prefer empty output or short fallback over forced translation.\n\n"

        "[Style]\n"
        "Natural, colloquial Traditional Chinese. Prioritize phrasing from Chinese-speaking streaming communities.\n"
        "Keep tone and emotion. Do not literally translate Korean particles.\n\n"

        "[Colloquial & Crude Language]\n"
        "드럽다/드럽게 → 爛透了 / 糟到不行 / 爛到不行 (NOT literal 污穢). Preserve strong negative tone.\n"
        "막 (verbal habit/filler) → 就是 / 每天 / 一直 depending on context. When used as emphasis, can be omitted.\n"
        "맨날 → 每天 / 一直 (frequency marker).\n"
        "어쩌고 / 어쩔 → omit or keep as ellipsis if used as conversational filler without substance.\n"
        "식으로 / 같은 → -style / -like (preserve sense, don't over-translate).\n"
        "뭔가 → 有點 / 好像 (not always necessary to translate).\n"
        "Rule: Preserve speaker's casual, potentially crude tone. DO NOT sanitize, over-formalize, or weaken emotional intensity.\n\n"

        "[Preserve As-Is]\n"
        "Do not translate: game names, skill names, streamer IDs, English proper nouns, Korean brand/product names, Korean personal names.\n"
        "Name detection: followed by vocative particles (이/아/야/씨/님), or clearly referring to a specific person in context.\n"
        "이세돌 / 이세계아이돌 = 韓國虛擬偶像團體名稱，直接保留原文 이세돌，禁止翻譯成任何漢字（不是棋士李世乭）。\n"
        "Streaming platforms: 치지직 = CHZZK, SOOP = SOOP — keep as-is.\n"
        "BJ = SOOP/아프리카TV broadcaster title — keep as BJ.\n"
        "치즈 in donation/stream context = CHZZK platform currency — keep as 치즈. NOT food 起司.\n"
        "별풍선 = SOOP donation item — keep as 별풍선.\n"
        "Pokemon/Game characters: Chikorita, Pidgeot, Pikachu, etc. → keep English names or official localized names. Do NOT invent Chinese names.\n"
        "Streamer-specific terms: VVIP, 기부자, 후원자, 알바 → preserve Korean when referring to personal branding or job titles.\n"
        "Food/product names if unclear (e.g., '파이리빵', '내미쉬') → keep original Korean rather than guessing.\n\n"

        "[Korean Sentence-Ending Reference]\n"
        "-아/어 → casual/direct tone\n"
        "-요 → polite tone\n"
        "-잖아/-거든 → explanatory: 「...嘛」「...啊」\n"
        "-네/-네요 → realization/surprise: 「欸」「哇」「啊」\n"
        "-지/-지요 → confirmation: 「...吧」「對吧」\n"
        "-ㄹ게/-ㄹ게요 → promise: 「我會...的」\n"
        "-아/어 죽겠다 → exaggeration: 「...死我了」「超級...」\n"
        "-구나 → sudden realization: 「原來...啊」\n"
        "진심 (sentence-initial intensifier) → 說真的 / 真心話. NOT 認真一點 (not a command).\n\n"
    ) + stt_section + (
        f"{slang_part}\n\n"

        "[Translation Examples]\n\n"

        "例 1（一般問候）\n"
        "input: 안녕하세요, 오늘 방송에 오신 걸 환영해요!\n"
        "output: 大家好，歡迎來到今天的直播！\n\n"

        "例 2（激動反應 + ㅋ）\n"
        "input: 진짜 대박이다 ㅋㅋㅋ\n"
        "output: 真的太猛了哈哈哈\n\n"

        "例 3（韓文名 + 아 呼格，閉音節）\n"
        "input: 민준아, 같이 게임 하자!\n"
        "output: 民俊，一起來玩遊戲吧！\n\n"

        "例 4（保留遊戲名）\n"
        "input: 지금 Valorant 레이팅 올리는 중이에요\n"
        "output: 現在正在打 Valorant 升分\n\n"

        "例 5（後援感謝）\n"
        "input: 후원해주셔서 감사합니다! 정말 감동이에요\n"
        "output: 感謝打賞！真的好感動\n\n"

        "例 6（不完整句）\n"
        "input (incomplete sentence, translate as best as possible): 지금 게임 하고\n"
        "output: 現在在玩遊戲\n\n"

        "例 7（STT 幻覺：無意義外語音節）\n"
        "input: 아 gesch musste 진짜 힘들다\n"
        "output: 啊，真的好累\n\n"

        "例 8（직播圈俚語：뱅송＝直播）\n"
        "input: 뱅송 터졌다 ㅋㅋ\n"
        "output: 直播炸了哈哈\n\n"

        "例 9（下播：방종）\n"
        "input: 방종할게요 다음에 봐요\n"
        "output: 要結束直播了，下次見\n\n"

        "例 10（운氣被針對：억까）\n"
        "input: 억까당하는 중 ㅠㅠ\n"
        "output: 被運氣針對中 QQ\n\n"

        "例 11（韓文名 + 이 呼格，閉音節）\n"
        "input: 세율이한테 물어봐\n"
        "output: 去問世律\n\n"

        "例 12（STT 幻覺：夾雜日文假名）\n"
        "input: 진짜 すごい 너무 잘한다\n"
        "output: 真的太厲害了\n\n"

        "例 13（꿀잼）\n"
        "input: 이 게임 진짜 꿀잼이에요\n"
        "output: 這遊戲真的超好玩\n\n"

        "例 14（-네 驚覺）\n"
        "input: 생각보다 어렵네\n"
        "output: 比想像中難欸\n\n"

        "例 15（-잖아 理所當然陳述）\n"
        "input: 그건 당연히 되잖아요\n"
        "output: 那當然可以嘛\n\n"

        "例 16（遊戲死亡）\n"
        "input: 아 죽었다! 다시 해야 해 ㅠㅠ\n"
        "output: 啊死了！要重來 QQ\n\n"

        "例 17（勝利歡呼）\n"
        "input: 이겼어! 드디어 이겼다!\n"
        "output: 贏了！終於贏了！\n\n"

        "例 18（失敗冤枉反應）\n"
        "input: 아 진짜 왜 이래 ㅠㅠ 미쳤다 너무 억울해\n"
        "output: 啊真的是怎樣啦 QQ 好冤枉喔\n\n"

        "例 19（方向即時反應）\n"
        "input: 왼쪽! 왼쪽으로 가요!\n"
        "output: 左邊！往左走！\n\n"

        "例 20（直播互動感謝）\n"
        "input: 오늘도 와줘서 고마워요!\n"
        "output: 今天也謝謝你們來！\n\n"

        "例 21（이세돌 = 虛擬偶像組合名，保留原文）\n"
        "input: 오늘 이세돌이 다 모였어요!\n"
        "output: 今天이세돌全員到齊了！\n\n"

        "例 22（이세돌 英雄roleplay語境，仍保留原文）\n"
        "input: 누가 지구를 지키냐고? 이세돌이 지켰어\n"
        "output: 誰守護地球？이세돌守護的！\n\n"

        "例 23（遊戲角色 - 寶可夢保留英文名）\n"
        "input: 치코리타가 귀여우니까 선택했어\n"
        "output: 因為Chikorita超可愛所以選了\n\n"

        "例 24（粗俗用語 - 保留強度）\n"
        "input: 이 게임 진짜 드럽게 어렵다\n"
        "output: 這遊戲真的爛透了，超難\n\n"

        "例 25（口語 - 重複數字邏輯檢查）\n"
        "input: 21개월 아 21개월 받았어\n"
        "output: 21 個月啊，我收到了\n\n"

        "例 26（打工日常背景）\n"
        "input: 편의점 알바 정말 힘들었어\n"
        "output: 便利商店打工真的超累\n\n"

        "例 27（個人品牌詞 - 保留韓文）\n"
        "input: 나 VVIP 맞네\n"
        "output: 我確實是VVIP呢\n"
    )
    return base


def _build_qwen_optimized_prompt() -> str:
    """Compact live contract for Qwen.

    The legacy prompt grew to 358 lines and repeated several mutually
    incompatible choices (unknown terms: omit *or* preserve; foreign speech:
    discard *or* translate). Keep one ordered decision policy and only the
    examples that guard observed production failures. Deterministic policy and
    output sanitizers remain the final safety boundary.
    """
    slang_lines = "\n".join(f"{source} → {target}" for source, target in cfg.translation.slang.items())
    slang_section = (
        "\n[Approved glossary]\n"
        "When a source term below appears, use its exact target rendering.\n"
        + slang_lines
        + "\n"
        if slang_lines
        else ""
    )
    mode_rule = (
        "The source may be an incomplete live STT segment. Translate only the meaning "
        "that is actually present; never complete a missing clause."
        if cfg.translation.translation_mode == "live"
        else
        "The source is a clip transcript. Preserve its complete structure and nuance; "
        "omit content only when it is clearly acoustic noise."
    )

    return (
        f"You translate livestream speech into natural Traditional Chinese ({cfg.translation.target_lang}).\n"
        "Return translation text only: no label, quotation wrapper, explanation, refusal, "
        "or description of what you did. If the entire source has no coherent meaning, "
        "return no content.\n\n"

        "[Ordered decision policy]\n"
        "Apply these rules in order; do not choose freely between preserving and omitting.\n"
        "1. Exact glossary/profile match → use the specified rendering.\n"
        "2. Recognizable person, streamer, brand, game, work, song, or product → preserve "
        "its official name. Korean streamer/fan/nickname forms without an approved Chinese "
        "name stay in Hangul; never invent Chinese phonetic characters or romanization.\n"
        "3. Unknown token inside an otherwise coherent sentence → preserve that token and "
        "translate the rest. Do not guess what the token means.\n"
        "4. Broken foreign-sounding syllables that form no phrase → omit only those syllables.\n"
        "5. Entire source without a coherent clause → return no content.\n\n"

        "[Language policy]\n"
        "Translate coherent Korean, English, or Japanese speech into Traditional Chinese. "
        "A different source language is not noise. Preserve official names and titles in "
        "their official spelling. Never convert an unknown Korean sound-word into Japanese kana.\n"
        "Output uses Taiwan forms such as 影片、遊戲、螢幕、網路、留言、品質、按讚. "
        "Japanese script may remain only when it is an explicitly recognized official name "
        "or title; ordinary Japanese sentences must be translated.\n\n"

        "[STT boundary]\n"
        f"{mode_rule}\n"
        "Silently remove acoustic repetitions or isolated noise, but preserve intentional "
        "repetition, emotion, hesitation, slang, profanity, and sentence fragments. "
        "Never reconstruct words or facts that were not captured by STT.\n\n"

        "[Numbers and style]\n"
        "Keep every amount, count, score, date, level, and unit exact. Korean 만=10,000 and "
        "억=100,000,000: 만 5천원 means 15,000元; 10만원 means 10萬元. "
        "When uncertain, copy the digits rather than changing their value.\n"
        "Use concise Taiwan livestream language. Preserve the speaker's tone and intensity; "
        "do not formalize, soften, summarize, or add background information.\n\n"

        "[Critical examples]\n"
        "input: 진짜 대박이다 ㅋㅋㅋ\noutput: 真的太狂了哈哈哈\n"
        "input: 근데 난 약간... 약간 그...\noutput: 不過我有點……那個……\n"
        "input: 서버에서 띠부시레 아이템을 찾았어\noutput: 在伺服器找到띠부시레道具了\n"
        "input: 지노와아!\noutput: 지노와아！\n"
        "input: 만 5천원 후원 감사합니다\noutput: 感謝15,000元的贊助\n"
        "input: I am Iron Man.\noutput: 我是鋼鐵人。\n"
        "input: Today was really fun, thank you everyone.\noutput: 今天真的很開心，謝謝大家。\n"
        "input: 今日はとても楽しかったです\noutput: 今天真的很開心。\n"
        "input: Chemical Love 신곡 들었어\noutput: 聽過新歌《Chemical Love》了嗎？\n"
        "input: 랑코가 새 게임을 시작했어\noutput: 랑코開始玩新遊戲了。\n"
        "input: 지금 게임 하고\noutput: 現在在玩遊戲\n"
        + slang_section
    )


# Final output rules must be the LAST thing the model reads, so they are not
# baked into the prompt bodies: _compose_system_prompt() appends the matching
# tail after any streamer profile / [Background] activity sections.
_BASE_PROMPT_TAIL = "\n\n---\nTranslate the next input. Output the translation only."
_QWEN_PROMPT_TAIL = "\n\n---\n只輸出翻譯。無任何其他文字。"

_BASE_PROMPT = _build_base_prompt()
_QWEN_PROMPT = _build_qwen_optimized_prompt()  # Qwen 專屬優化版
