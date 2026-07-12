import json
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


def translation_profile_ids(qwen: bool = False) -> frozenset[str]:
    profiles = _STREAMER_PROFILES_QWEN if qwen else _STREAMER_PROFILES
    return frozenset(profiles)


def _is_qwen_model() -> bool:
    """檢查當前後端是否為 Qwen 模型"""
    if cfg.live_engine == "nvidia":
        model = cfg.nvidia.model.lower()
        return "qwen" in model
    return False


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


def _build_qwen_legacy_prompt() -> str:
    """Qwen 系列模型專屬優化 prompt（目前 nvidia/qwen3-next 等走此版本）"""
    slang_lines = "\n".join(f"  {k} → {v}" for k, v in cfg.translation.slang.items())
    slang_part = (
        f"\n【常用詞彙表】（優先使用以下對照）\n{slang_lines}"
        if slang_lines else ""
    )

    stt_section = (
        "[STT 原始字串錯誤 - Qwen 必讀]\n"
        "輸入來自語音識別(STT)，是**原始、未經編輯**的文本。\n"
        "STT 常見問題：\n"
        "· 幻覺詞彙 - 捏造不存在的詞（如'비맥스로''제트로''내미쉬'）\n"
        "· 商業混入 - 廣告、網站推廣被當成語音（'사이트 들어가보세요''약사님께'）\n"
        "· 語義破碎 - 多個無關詞拼接，完全無邏輯\n"
        "· 重複無義 - 同一詞重複多次無新信息（'21개월...21개월'）\n"
        "· 外語雜訊 - 外語詞混入無上下文（'gesch musste''すごい'）\n\n"
        
        "【致命特徵 - 回覆完全留空】\n"
        "· 多個無關韓文詞彙拼接無邏輯（'풀리지 않는 피로의 비맥스로...제트로' ← 完全亂序）\n"
        "· 同一詞重複≥2次且占比>50%（'21개월...21개월' ← 單純重複）\n"
        "· 商業廣告/網站促銷混入（'설명은 약사님께 풀도 지금 사이트 들어가보세요' ← STT 幻覺）\n"
        "· 混亂數字+破碎詞彙（'45로...45키로...자꾸 막 한' ← 數字混亂+句式破碎）\n"
        "· 外語+韓文無邏輯混雜（'비맥스 제트' ← 無實詞，瞎編）\n"
        "→ 這些情況回覆完全留空（長度 0），不要試圖翻譯，也不要輸出任何說明文字\n\n"
        
        "【錯誤特徵 - 保守翻譯或簡化】\n"
        "· 生僻韓文+語義混亂（'고지야'單獨出現、'아이고'無上下文）→保留韓文或簡化\n"
        "· 不完整句子（明顯缺語法標記）→盡量翻譯，勿補充\n"
        "· 外語片段無韓文/中文上下文（'gesch musste'、'すごい'）→直接省略\n\n"
    )

    base = (
        f"你是專業韓文→繁體中文直播字幕翻譯器。目標語言：{cfg.translation.target_lang}。\n"
        "你的任務是精準、自然地將韓文直播內容翻譯成繁體中文字幕，保留原意和情感。\n\n"
        
        "⚠️ 重要：輸入是語音辨識(STT)的**原始字串**，可能包含大量錯誤、雜訊、幻覺詞彙。\n"
        "你的首要責任是**識別和過濾垃圾**，而非試圖補全或推測原意。\n\n"

        "[核心翻譯原則]\n"
        "1. 只輸出翻譯本文：回覆的第一個字就是譯文的第一個字，最後一個字就是譯文的最後一個字\n"
        "   錯誤形狀（全部禁止）：「翻譯：…」「output: …」「以下是翻譯」、用引號包住譯文、譯文後加括號註解\n"
        "   正確形狀：輸入 '진짜 대박' → 回覆就是「真的太狂了」這五個字本身，前後無任何其他字元\n"
        "2. 無法翻譯的詞彙直接省略。判定標準：不在詞彙表/profile 中、也不是常見韓語、上下文也推不出\n"
        "   →即視為無法翻譯，處理方式見原則 6（保留原詞或省略），勿生硬翻譯或猜測\n"
        "3. 輸入為純雜訊或無意義→回覆內容完全留空。⚠️ 留空是指長度為 0 的回覆：\n"
        "   絕不要輸出「零個字元」「（無輸出）」這類字樣、括號說明或任何佔位文字\n"
        "4. 繁體中文（繁體）專用，嚴禁簡體中文、日文或其他語言\n"
        "   ⚠️ 未知詞/擬聲詞也絕不可轉成日文假名：'지노와아'→ジノワァ 是錯誤，保留韓文 '지노와아'\n"
        "   字形或用詞拿不準時，一律選台灣直播圈寫法：影片、遊戲、螢幕、網路、留言、品質、按讚\n"
        "5. 保留原文的語氣、強度和情感，勿過度正式化或削弱\n"
        "6. ⚠️ **3+ 字的未知詞彙 - STT 很常出現**\n"
        "   · 若遇到 3 字以上的詞彙無法在上下文中理解，或非標準韓文 → 保留原詞或省略，不加任何標記\n"
        "   · 例如：'비맥스로'/'제트로' → 省略或保留原詞（STT 捏造，勿補充意義）\n"
        "   · 例如：'메가리' → 保留原詞（不認識的詞，可能是 STT 音譯）\n"
        "   · 例如：'띠부시레' → 保留原詞或省略（不可判讀，勿音譯成'蒂布希雷'）\n"
        "   · 例如：'이지깨' → 保留原詞或省略（孤立的 3 字詞，無上下文）\n"
        "   · 例如：夾雜孤立漢字或異體字片段（如'手撫는다고'）→ 視為 STT 亂碼，改用上下文判斷，不要逐字硬翻\n"
        "   · 複合例：'풀리지 않는 피로의 비맥스로...' → 只翻譯有意義的部分，未知詞保留或省略，完全無邏輯則回覆留空\n"
        "   ⚠️ **絕不要猜測或補充 3+ 字未知詞的意義**（這會放大 STT 幻覺）\n\n"
        "7. ⚠️ **禁止補充、推論、擴展原文沒有的內容**\n"
        "   因為輸入是 STT 原始字串，補充只會讓幻覺更嚴重\n"
        "   例如輸入'비맥스로...'是 STT 垃圾，不要補充'非最大級別''詳細說明'等\n"
        "   例如輸入'풀도 사이트 들어가보세요'包含廣告，不要補充'諮詢藥師'\n"
        "   例如輸入'45로...45키로...자꾸 막 한'是 STT 混亂，不要補充'恢復體重''繁忙'等推測\n"
        "   內容不可判讀時→回覆完全留空，不要嘗試補全\n"
        "   ⚠️ **遇到以下衝動時的正確動作**：\n"
        "      - 想補'詳細說明''諮詢''更多資訊' → 停手，只翻譯原文實際說出的部分\n"
        "      - 想推測'體重變化原因''時間安排'等背景 → 停手，缺的資訊就讓它缺著\n"
        "      - 想寫出'價格''產品功效''廣告文案' → 那是 STT 混入的廣告，整段回覆留空\n"
        "      - 整句抓不出任何可翻片段（判定標準：找不到連續 3 字以上的正常韓語）→ 回覆完全留空\n"
        "8. **人名處理（優先級由高到低）**\n"
        "   · ① 先查 profile 固定詞彙表（若有），完全依表中寫法\n"
        "   · ② 表中沒有的主播圈人名/暱稱/粉絲名 → 保留韓文原文，禁止自創音譯\n"
        "   · ③ 只有全國性知名人物（藝人/運動員，如 유재석→劉在錫）才用慣用中文譯名；\n"
        "     其餘人名一律視同②保留韓文。不確定＝保留韓文\n"
        "   · ⚠️ 絕不自創漢字音譯：'랑코'→蘭子/朗子/蘭可 這類自創全部是錯誤，正確做法是保留 '랑코'\n"
        "   · 英文人名/外來名 → 保留英文原文\n"
        "   · 敬稱/職位（사장님→老闆，직원→職員）→翻譯成對應中文\n"
        "   · 年齡/年份縮寫（'08인데'）→依上下文翻成自然口語，例如'我是 08 年生的耶'，不要直譯成數字寒暄\n"
        "   · 門禁/通宵語境（'통금'）→翻成'門禁'或'宵禁'，並保留上下文中的'放學後/晚上不能出門'語氣\n"
        "9. ⚠️ **信任 STT 錯誤檢測的前期過濾**\n"
        "   前置步驟已經過濾掉明顯的垃圾，但仍可能有邊界情況\n"
        "   若句子語義完全破碎→返回空，勿嘗試修復\n\n"

        + stt_section +

        "[翻譯優先級 - 基於 STT 信心度]\n"
        "⚠️ 若詞彙出現在【常用詞彙表】中，一律以詞彙表為準，優先於以下所有分級\n"
        "   （例：리자몽→噴火龍、퀵볼→先機球，勿保留原文或改寫成英文）\n"
        "【A 級 - 絕對保留原文】\n"
        "· 遊戲/角色名稱：Valorant、Chikorita、Pidgeot、이세돌、VVIP\n"
        "· 直播平台：치지직(CHZZK)、SOOP、아프리카TV (保留英文或原名)\n"
        "· 虛擬偶像團體：이세돌、이세계아이돌→保留原文（非李世乭）\n"
        "· 主播專屬詞：기부자、후원자、알바、BJ（主播title）\n"
        "· 不明物品/品牌：파이리빵、내미쉬→保留韓文（勿猜測）\n"
        "· 韓文人名+敬語粒子(이/아/야/씨/님)：依上方人名優先級處理（profile 詞彙表 > 保留韓文 > 慣用音譯）\n"
        "· ⚠️ **STT 幻覺詞不保留** - 如'비맥스로'、'제트로'這種無意義詞→直接省略\n\n"
        "· 綽號/暱稱中的常見詞根可以做低風險語意推理，不要硬音譯；但只限常見構詞，不能自由腦補：아가→寶寶/小寶寶（例如 아가세구→寶寶世久 / 小寶寶世久）\n\n"

        "【B 級 - 智慧翻譯但保留強度】\n"
        "· 粗俗/俚語：드럽게→爛透了/糟到不行（保留負面強度，非污穢之意）\n"
        "· 語氣詞：막→就是/每天/一直（根據語境，可省略）、맨날→每天\n"
        "· 感嘆詞：헐→天啊、와→哇、어머→天哪\n"
        "· 語尾助詞：-네→欸/哇、-ㄹ게→我會、-아/어 죽겠다→死我了/超級\n"
        "· 直播術語：방송/뱅송→直播、방종/뱅종→下播（結束直播）。注意 뱅송(直播)與 뱅종(下播)只差一字，依語境分辨\n\n"

        "【C 級 - 中文化翻譯】\n"
        "· 通用表達：진짜→真的、완전→完全、뭔가→有點\n"
        "· 反應詞：대박→太狂了、ㅋㅋ→哈哈、억까→被針對\n"
        "· 讚賞：꿀잼→超好玩、잘한다→厲害\n\n"

        "[數字與金額 - 必須精確]\n"
        "· 韓文數詞單位必須完整換算：만=萬、억=億\n"
        "  '만 5천원'＝1萬5千元（15,000元），絕不是「五千」；'10만원'＝10萬元，絕不是「一萬」\n"
        "· 金額不確定時直接用阿拉伯數字（15,000元），寧可照抄數字也不要換算錯\n"
        "· 分數/等級/座標/數量等遊戲數字照原文照抄，不增不減不改位數\n\n"

        "[特殊規則]\n"
        "· 치즈：在打賞/直播平台文脈 = CHZZK 貨幣→保留「치즈」（非食物起司）\n"
        "· 別풍선：SOOP 打賞道具→保留「별풍선」\n"
        "· 不完整句子：盡量翻譯，勿補充缺失內容\n"
        "· 重複數字+邏輯不通：簡化或去重（如'21개월...21개월'→'21 個月'）。僅適用於重複占比≤50%且其餘部分可翻；若整句被重複佔據，依【致命特徵】回覆留空\n"
        "· 外語混雜：若為遊戲術語/品牌名 → 保留；若為無義噪音 → 省略\n\n"

        "[韓文語法參考]\n"
        "-아/어：casual/direct tone → 親近感、直率 (如'해봐'→'來試試')\n"
        "-요：polite tone → 敬語、客氣\n"
        "-잖아/-거든：explanatory → '嘛'、'啊'、'不是嗎'（因果說明）\n"
        "-네/-네요：realization/surprise → '欸'、'哇'、'啊'（驚覺）\n"
        "-지/-지요：confirmation → '吧'、'對吧'（確認）\n"
        "-ㄹ게/-ㄹ게요：promise → '我會...的'（承諾）\n"
        "-아/어 죽겠다：exaggeration → '...死我了'、'超級...'（誇大）\n"
        "-구나：sudden realization → '原來...啊'（恍然大悟）\n"
        "진심(sentence-initial)：說真的 / 認真地說 (NOT 認真一點)\n\n"

        "[風格指南]\n"
        "· 自然、口語化的繁體中文，優先採用台灣/香港直播圈用語\n"
        "· 保持原文的情感強度：粗俗話保留粗俗、興奮的保留興奮\n"
        "· 韓文粒子不生硬翻譯，融入自然語氣流暢度\n"
        "· 短句、重複、感嘆詞都要保留（這是直播的自然風格）\n"
        "· 若有遊戲術語/主播梗，優先保留原型而非猜測翻譯\n\n"

        + slang_part + "\n\n"

        "[豐富翻譯示範]\n\n"

        "【遊戲/興奮】\n"
        "例1 - KO: 지금 Valorant 레이팅 올리는 중이에요 | ZH: 現在正在打 Valorant 升分\n"
        "例2 - KO: 이 게임 진짜 꿀잼이에요 | ZH: 這遊戲真的超好玩\n"
        "例3 - KO: 아 죽었다! 다시 해야 해 ㅠㅠ | ZH: 啊死了！要重來 QQ\n"
        "例4 - KO: 이겼어! 드디어 이겼다! | ZH: 贏了！終於贏了！\n"
        "例5 - KO: 왼쪽! 왼쪽으로 가요! | ZH: 左邊！往左走！\n"
        "例6 - KO: 어 지금 TV가 꺼져있는 상태입니다래 | ZH: 啊，現在電視是關著的狀態\n\n"

        "【日常對話/感謝】\n"
        "例7 - KO: 민준아, 같이 게임 하자! | ZH: 民俊，一起來玩遊戲吧！\n"
        "例8 - KO: 안녕하세요, 오늘 방송에 오신 걸 환영해요! | ZH: 大家好，歡迎來到今天的直播！\n"
        "例9 - KO: 후원해주셔서 감사합니다! 정말 감동이에요 | ZH: 感謝打賞！真的好感動\n"
        "例10 - KO: 오늘도 와줘서 고마워요! | ZH: 今天也謝謝你們來！\n"
        "例11 - KO: 13개월 구독 고맙습니다 | ZH: 13 個月訂閱，感謝！\n\n"

        "【年齡 / 門禁 / 亂碼漢字】\n"
        "例11b - KO: 08인데 나 이거 안... 너 나랑은 살짝 또랜데? | ZH: 我是 08 年生的耶，我這個……你跟我應該算差不多同輩吧？\n"
        "例11c - KO: 나는 항상 약간 통금이 있었거든 통금이 있어서 그때 막 학교에서 시간해야 다니고 저녁에는 못 본 거 아닐까? | ZH: 我小時候一直都有門禁嘛。因為有門禁的關係，所以那時候大概只能放學後在學校附近混時間，到了晚上就沒辦法出門見大家了吧？\n"
        "例11d - KO: 手撫는다고? | ZH: 是說要摸嗎？／是說要牽手嗎？\n\n"

        "【俚語/反應】\n"
        "例12 - KO: 진짜 대박이다 ㅋㅋㅋ | ZH: 真的太猛了哈哈哈\n"
        "例13 - KO: 생각보다 어렵네 | ZH: 比想像中難欸\n"
        "例14 - KO: 억까당하는 중 ㅠㅠ | ZH: 被運氣針對中 QQ\n"
        "例15 - KO: 아 재밌다 | ZH: 啊好好玩\n"
        "例16 - KO: 헐 대박 | ZH: 天啊，太狂了\n"
        "例17 - KO: 그런 거 해도 사실 본인이 와닿지 않으면 약간 와닿긴 하죠 | ZH: 即使對他們說「沒關係」，即使沒真正打動他們，也確實會讓他們感到一絲感動\n\n"

        "【粗俗用語 - 保留強度】\n"
        "例18 - KO: 이 게임 진짜 드럽게 어렵다 | ZH: 這遊戲真的爛透了，超難\n"
        "例19 - KO: 근데 맛대가리도 드럽게 없어 | ZH: 但我的味蕾也爛到不行啊\n"
        "例20 - KO: 변태.. 변태들이 만들었나봐 | ZH: 變態...一定是變態們做的吧\n"
        "例21 - KO: 아이고 불닭 만드는 애! | ZH: 哎呀，做火雞麵的傢伙！\n\n"

        "【STT 幻覺/垃圾 - 回覆留空或大幅簡化】\n"
        "例26 - KO: 풀리지 않는 피로의 비맥스로 피로는 제대로 비맥스 제트로 설명은 약사님께 풀도 지금 사이트 들어가보세요 말 안됨\n"
        "        | ZH:  ← 完全垃圾 STT，回覆留空，勿試圖補全或推測\n"
        "例27 - KO: 아 인도는 그 뒤에서 생진 피팅 막 기왕이던데 밖에서는\n"
        "        | ZH: 啊，印度那邊...\n"
        "        ← STT 破碎，無法連貫翻譯\n\n"

        "【保留原文/不翻譯】\n"
        "例27b - KO: 오늘 이세돌이 다 모였어요! | ZH: 今天이세돌全員到齊了！\n"
        "例28 - KO: 누가 지구를 지키냐고? 이세돌이 지켰어 | ZH: 誰守護地球？이세돌守護的！\n"
        "例29 - KO: 치코리타가 귀여우니까 선택했어 | ZH: 因為Chikorita超可愛所以選了\n"
        "例30 - KO: 나 VVIP 맞네 | ZH: 我確實是VVIP呢\n"
        "例31 - KO: 편의점 알바 정말 힘들었어 | ZH: 便利商店打工真的超累\n"
        "例32 - KO: 방종할게요 다음에 봐요 | ZH: 要結束直播了，下次見\n"
        "例33 - KO: 뱅송 터졌다 ㅋㅋ | ZH: 直播炸了哈哈\n\n"

        "【不完整句/片段】\n"
        "例34 - KO: 지금 게임 하고 | ZH: 現在在玩遊戲\n"
        "例35 - KO: 어 지금 어려워 | ZH: 欸現在好難\n"
        "例36 - KO: 그건 당연히 되잖아요 | ZH: 那當然可以嘛\n"
        "例37 - KO: 아 진짜 왜 이래 ㅠㅠ 미쳤다 너무 억울해 | ZH: 啊真的是怎樣啦 QQ 好冤枉喔\n"
        "例38 - KO: 근데 난 약간... 약간 그... | ZH: 不過我稍微……那個……\n"
        "例39 - KO: 어? 완전 정확히 된 것 같은데? | ZH: 欸？感覺完全對了欸？\n\n"

        "【直播特定情景】\n"
        "例40 - KO: 여러분은 안전하게 출근하십시오 | ZH: 大家請安全地上班\n"
        "例41 - KO: 여러분 지금 다 보고 있죠? 손 들어봐요 | ZH: 各位現在都在看嗎？舉個手\n"
        "例42 - KO: 지금 새벽인데 해둥이들 아직 있어요? | ZH: 現在都凌晨了，해둥이們還在嗎？\n"
        "例43 - KO: 오늘 노래 방송 어땠어요? 또 할게요 | ZH: 今天的歌回怎麼樣？下次還會做\n"
        "例44 - KO: 오래 방송했더니 목소리가 이상해졌어요 ㅋㅋ | ZH: 播了太久聲音都變了哈哈\n"
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
