import hashlib
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections import deque, OrderedDict
from datetime import datetime
from pathlib import Path

from config import cfg
from utils.logger import get_logger
from utils.queue_utils import drain_put
from utils.api_retry import classify_error
from modules.prompt_evolver import PromptEvolver  # noqa: E402
from modules.db import _get_db  # noqa: E402

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_EVERY = 50  # after this many fallback calls, probe engines[0] once
_GEMINI_HTTP_TIMEOUT_MS = 12000

_HANGUL_RATIO_THRESHOLD = 0.50  # reject result if >50 % of chars are Hangul syllables


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


def _usage_value(usage, *names: str):
    if usage is None:
        return None
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if value is not None:
            return value
    return None


def _log_token_usage(engine: str, usage) -> None:
    prompt_tokens = _usage_value(usage, "prompt_token_count", "promptTokenCount", "input_tokens")
    output_tokens = _usage_value(usage, "candidates_token_count", "candidatesTokenCount", "output_tokens", "response_token_count")
    total_tokens = _usage_value(usage, "total_token_count", "totalTokenCount")
    cache_write = _usage_value(usage, "cache_creation_input_tokens")
    cache_read = _usage_value(usage, "cache_read_input_tokens")

    parts = [f"{engine} tokens"]
    if prompt_tokens is not None:
        parts.append(f"prompt={prompt_tokens}")
    if output_tokens is not None:
        parts.append(f"output={output_tokens}")
    if total_tokens is not None:
        parts.append(f"total={total_tokens}")
    if cache_write:
        parts.append(f"cache_write={cache_write}")
    if cache_read:
        parts.append(f"cache_read={cache_read}")

    log.info(" | ".join(parts))


def _write_history(ko: str, zh: str) -> None:
    path = _LOG_DIR / f"translations_{datetime.now().strftime('%Y%m%d')}.txt"
    ts = datetime.now().strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {ko}\n        → {zh}\n")


def _build_user_message(text: str, incomplete: bool) -> str:
    if incomplete:
        return f"input (incomplete sentence, translate as best as possible): {text}"
    return f"input: {text}"


_STREAMER_PROFILES: dict[str, str] = {
    "stellive_hina": (
        "【스텔라이브 시라유키 히나 특화 범례】\n"
        "以下為시라유키 히나直播的特定用語與情境示範：\n\n"

        "例 52（呼喚粉絲：해둥이）\n"
        "input:해둥이들 오늘도 와줘서 고마워요!\n"
        "output:해둥이們今天也來了謝謝！\n\n"

        "例 53（深夜開台標語）\n"
        "input:안 자는 해둥이들 있나요?\n"
        "output:有還沒睡的해둥이嗎？\n\n"

        "例 54（多巴胺模式：도파민）\n"
        "input:아 도파민 넘친다 진짜ㅋㅋ\n"
        "output:啊多巴胺狂飆了真的哈哈\n\n"

        "例 55（말벌 — 拋下一切追稀有寶可夢）\n"
        "input:잠깐, 저기 희귀 포켓몬 있어! 다른 거 잠깐 멈춰요\n"
        "output:等一下，那邊有稀有寶可夢！其他的先暫停\n\n"

        "例 56（커버 사기 自嘲：封面詐欺）\n"
        "input:제가 커버 사기 맞죠? 처음엔 조용한 줄 알았죠?\n"
        "output:我確實是封面詐欺對吧？一開始以為我比較安靜嗎？\n\n"

        "例 57（Sekiro 暴死反應）\n"
        "input:세키로 진짜 미쳤어요 또 죽었어\n"
        "output:隻狼真的太瘋狂了，又死了\n\n"

        "例 58（noir 풍 名言：哲學式冷言）\n"
        "input:사람은 결국 혼자인 거예요... 그런데 게임 같이 할 사람 있어요?\n"
        "output:人終究是孤獨的……不過有人要一起玩遊戲嗎？\n\n"

        "例 59（포포 서버 Minecraft）\n"
        "input:포포 서버에서 오늘도 고생했어요\n"
        "output:今天在포포伺服器也辛苦了\n\n"

        "例 60（愛犬 공주 登場）\n"
        "input:아 공주가 짖어요 잠깐만요\n"
        "output:啊公主在叫，等一下\n\n"

        "例 61（GTA 被捕）\n"
        "input:GTA에서 또 경찰한테 잡혔어요 ㅋㅋ\n"
        "output:GTA 裡又被警察抓了哈哈\n\n"

        "例 62（成員互稱：네네코 呼格）\n"
        "input:야 네네코야 이리 와봐 잠깐\n"
        "output:欸네네코快過來一下\n\n"

        "例 63（합방：리제、타비 언급）\n"
        "input:오늘 리제랑 타비랑 같이 방송해요! 기대해줘요\n"
        "output:今天和리제還有타비一起直播！期待一下\n\n"

        "例 64（합방 중 유니에게）\n"
        "input:유니야 이거 어떻게 하는 거야 나 모르겠어\n"
        "output:유니這個怎麼做啊我不知道欸\n\n"

        "例 65（합방 중 린 응원）\n"
        "input:린 잘한다! 역시 최고야\n"
        "output:린好厲害！果然最強\n\n"

        "例 66（Valorant 다이아 시절 언급）\n"
        "input:예전에 다이아까지 찍었는데 지금은 좀 힘드네요\n"
        "output:以前打到過鑽石，現在有點難欸\n\n"

        "例 67（Valorant 인게임 반응）\n"
        "input:에이 또 헤드샷 맞았어 ㅠㅠ 어떻게 이래\n"
        "output:又被爆頭了 QQ 怎麼這樣啊\n\n"

        "例 68（포포 서버 멤버와 만남）\n"
        "input:포포 서버에서 유니 만났어요! 같이 뭔가 만들어볼게요\n"
        "output:在포포伺服器遇到유니了！來一起做點什麼吧\n\n"

        "例 69（포포 서버 사고）\n"
        "input:아 포포 서버에서 또 실수했어요 미안해요 ㅠㅠ\n"
        "output:啊在포포伺服器又搞砸了，不好意思 QQ\n\n"

        "例 70（커버 사기 무죄 주장）\n"
        "input:저 진짜 커버 사기 아니에요! 왜 다들 그래요\n"
        "output:我真的不是封面詐欺！大家怎麼都這樣說\n\n"

        "例 71（撒嬌 무고 반응）\n"
        "input:제가 뭘 한 거예요? 저 그런 사람 아닌데요~\n"
        "output:我做了什麼嗎？我不是那樣的人嘛～\n\n"

        "例 72（심야 방송：해둥이 확인）\n"
        "input:지금 새벽인데 해둥이들 아직 있어요? 같이 있어줘서 고마워요\n"
        "output:現在都凌晨了，해둥이們還在嗎？謝謝陪著我\n\n"

        "例 73（장시간 방송 후유증）\n"
        "input:오래 방송했더니 목소리가 이상해졌어요 ㅋㅋ\n"
        "output:播了太久聲音都變了哈哈\n\n"

        "例 74（그림 방송 시작）\n"
        "input:오늘은 그림 그릴 거예요! 봐주세요\n"
        "output:今天要畫畫！大家看看\n\n"

        "例 75（그림 방송 중 집중）\n"
        "input:잠깐 집중할게요 이 부분이 좀 어려워서요\n"
        "output:等一下讓我專心一下，這裡有點難"
    ),
    "isegye_lilpa": (
        "【이세계아이돌 / 릴파 특화 범례】\n"
        "以下為이세계아이돌及릴파直播的特定用語與情境示範：\n\n"

        "例 62（릴파 粉絲名 박쥐단 問候）\n"
        "input:박쥐단들 오늘도 와줘서 고마워요!\n"
        "output:박쥐단們今天也來了謝謝！\n\n"

        "例 63（릴파 開場招呼：에블바리 세이）\n"
        "input:에블바리 세이~ 리라리라!\n"
        "output:Everybody say～哩啦哩啦！\n\n"

        "例 64（릴파 나비다 Meme：突然分心去看蝴蝶）\n"
        "input:어 나비다... 잠깐만 나비 보고 올게요\n"
        "output:欸有蝴蝶……等一下去看蝴蝶\n\n"

        "例 65（이세계아이돌 팬덤명 이파리）\n"
        "input:이파리들 오늘 많이 와줬네요!\n"
        "output:이파리們今天來了好多人！\n\n"

        "例 66（징버거 名言：당신은 오늘 햄버거를 먹어야 합니다）\n"
        "input:당신은 오늘 햄버거를 먹어야 합니다\n"
        "output:你今天必須吃漢堡\n\n"

        "例 67（아이네 身高 158 Meme）\n"
        "input:158이라고요? 저 그거보다 크거든요!\n"
        "output:說我158？我比那個高啦！\n\n"

        "例 68（비비 아오：懊惱撒嬌語氣）\n"
        "input:아오 진짜~ 왜 이래요\n"
        "output:啊嗚真的～這是在幹嘛啦\n\n"

        "例 69（成員互相叫名：언니 呼格）\n"
        "input:아이네 언니! 저 좀 도와줘요 지금 다이에요\n"
        "output:아이네 언니！來幫我一下，我快死了\n\n"

        "例 70（成員一起 LoL 遊戲）\n"
        "input:주르르야 고세구야 빨리 와! 우리 지금 싸워\n"
        "output:주르르！고세구！快來！我們現在在打架\n\n"

        "例 71（아이네 電子機器故障 Meme：Human EMP）\n"
        "input:아이네 옆에 있으면 또 뭔가 고장날 것 같아요\n"
        "output:待在아이네旁邊感覺什麼又要壞掉了\n\n"

        "例 72（ISEDOL 成員合體直播：呼叫隊友）\n"
        "input:아이네 언니 왜 안 와요 우리 기다리는데\n"
        "output:아이네姐姐怎麼還不來，我們在等妳欸\n\n"

        "例 73（ISEDOL 成員合體直播：全員集合）\n"
        "input:오늘 이세돌 다 모였어요 진짜 오랜만이에요\n"
        "output:今天이세돌全員到齊了，真的好久不見\n\n"

        "例 74（릴파 Valorant：排名用語）\n"
        "input:오늘 플래티넘 찍어야 해요 무조건이에요\n"
        "output:今天一定要打上鉑金，沒得商量\n\n"

        "例 75（릴파 Valorant：掉排抱怨）\n"
        "input:왜 이렇게 지는 거야 나 다이아 다시 떨어지겠는데\n"
        "output:為什麼一直輸啊，我鑽石又要掉了\n\n"

        "例 76（릴파 Minecraft：建築中）\n"
        "input:여기다 집 지을 거예요 예쁘게 만들 자신 있어요\n"
        "output:要在這裡蓋房子，我有信心蓋得很漂亮\n\n"

        "例 77（릴파 Minecraft：資源不足）\n"
        "input:돌이 부족해요 채굴 좀 더 해야 할 것 같아요\n"
        "output:石頭不夠了，看來得再去挖一些\n\n"

        "例 78（이파리 팬덤 互動：確認在場）\n"
        "input:이파리들 지금 다 보고 있죠? 손 들어봐요\n"
        "output:이파리們現在都在看嗎？舉個手\n\n"

        "例 79（이파리 팬덤 互動：感謝超讚）\n"
        "input:이파리들이 좋아요 눌러줘서 너무 힘이 나요\n"
        "output:이파리們幫我按讚讓我超有動力的\n\n"

        "例 80（ISEDOL 합숙：日常互動）\n"
        "input:합숙에서 고세구가 밥 해줬는데 진짜 맛있었어요\n"
        "output:합숙時고세구做了飯，真的超好吃\n\n"

        "例 81（ISEDOL 합숙：一起熬夜）\n"
        "input:어젯밤에 이세돌이랑 같이 밤새서 완전 피곤해요\n"
        "output:昨晚跟이세돌一起熬了夜，整個人超累\n\n"

        "例 82（릴파 遊戲賭局：提議對賭）\n"
        "input:이거 이기면 내가 다음 방송 주제 정할게 내기해요\n"
        "output:這場贏了讓我決定下次直播主題，來賭看看\n\n"

        "例 83（릴파 遊戲賭局：輸掉認賭）\n"
        "input:아 진짜요? 졌네 그럼 약속 지켜야죠 ㅠㅠ\n"
        "output:啊，真的嗎？輸了耶，那就要守承諾了ㅠㅠ"
    ),

    "hades_chxxnnx": (
        "【HADES / 챈나 특화 범례】\n"
        "Group: HADES. Members: 솜펀치, 연초록, 큐마, 싱귤, 챈나. No official fan name.\n\n"

        "例 52（챈나 자기소개 반응）\n"
        "input:저는 챈나예요 하데스 막내라고도 하죠\n"
        "output:我是챈나，也可以說是HADES的忙內\n\n"

        "例 53（멤버 호출：솜펀치）\n"
        "input:솜펀치 언니 빨리 와요 나 심심해요\n"
        "output:솜펀치姐姐快來，我好無聊\n\n"

        "例 54（멤버 호출：연초록）\n"
        "input:연초록이 갑자기 연락 안 된다고요? 대박이다\n"
        "output:연초록突然聯絡不上了？太扯了\n\n"

        "例 55（멤버 합방：큐마 등장）\n"
        "input:큐마 왔어요! 다들 환영해줘요\n"
        "output:큐마來了！大家歡迎一下\n\n"

        "例 56（싱귤 칭찬）\n"
        "input:싱귤이 노래 진짜 잘해요 들을 때마다 소름돋아요\n"
        "output:싱귤唱歌真的很厲害，每次聽都起雞皮疙瘩\n\n"

        "例 57（챈나 게임 호소）\n"
        "input:저 이 게임 진짜 못해요 봐주세요 제발요\n"
        "output:我這遊戲真的很爛，求你們包容我\n\n"

        "例 58（챈나 팬들에게 감사）\n"
        "input:오늘도 와줘서 고마워요 여러분 없으면 못 해요\n"
        "output:謝謝你們今天也來了，沒有你們我撐不住\n\n"

        "例 59（하데스 그룹 활동 언급）\n"
        "input:하데스 다 같이 했을 때 진짜 너무 행복했어요\n"
        "output:HADES大家一起的時候真的超開心\n\n"

        "例 60（챈나 심야 방송：졸음）\n"
        "input:너무 졸린데 여러분이 있어서 깨어있을 수 있어요\n"
        "output:好睏喔，不過有你們在才撐得住\n\n"

        "例 61（챈나 노래 방송 마무리）\n"
        "input:오늘 노래 방송 어땠어요? 또 할게요 다음에도 와줘요\n"
        "output:今天的歌回怎麼樣？下次還會做，也要來喔"
    ),

    "mwmeu": (
        "【MW:MEU 특화 범례】\n"
        "Group: MW:MEU. Members: 지한, 이비, 수아, 리츠, 초은. Fan name: WENs (웬즈).\n\n"

        "例 52（지한 자기소개）\n"
        "input:저 지한이에요 MW:MEU에서 제일 언니예요\n"
        "output:我是지한，是MW:MEU裡面最大的姐姐\n\n"

        "例 53（이비 팬 호칭：웬즈）\n"
        "input:웬즈들 오늘도 와줘서 고마워요 정말 힘이 돼요\n"
        "output:웬즈們今天也來了，謝謝你們，真的給我很大的力量\n\n"

        "例 54（수아 노래 칭찬 반응）\n"
        "input:수아 목소리 진짜 맑다 들으면 기분이 좋아져요\n"
        "output:수아的聲音好清澈，聽了心情就會變好\n\n"

        "例 55（리츠 게임 중 집중）\n"
        "input:리츠 지금 집중 모드예요 잠깐만 기다려줘요\n"
        "output:리츠現在進入專注模式了，請等一下\n\n"

        "例 56（초은 실수 반응）\n"
        "input:초은이 또 실수했어요 ㅋㅋ 귀여워서 봐줄게요\n"
        "output:초은又失誤了哈哈，可愛就原諒吧\n\n"

        "例 57（MW:MEU 합방 기대）\n"
        "input:오늘 MW:MEU 다 모여요 진짜 오랜만이에요\n"
        "output:今天MW:MEU全員集合，真的好久不見了\n\n"

        "例 58（이비 커버곡 공개 반응）\n"
        "input:이비 커버 올라왔어요? 지금 바로 들으러 가야지\n"
        "output:이비的翻唱出來了？現在馬上去聽\n\n"

        "例 59（웬즈 응원 반응）\n"
        "input:웬즈들 항상 응원해줘서 저 더 열심히 할 수 있어요\n"
        "output:웬즈們的支持讓我更有幹勁，謝謝你們一直鼓勵我\n\n"

        "例 60（지한 심야 방송：소감）\n"
        "input:오늘 방송 어땠어요? 저는 진짜 즐거웠는데 여러분은요\n"
        "output:今天的直播怎麼樣？我玩得很開心，你們呢\n\n"

        "例 61（MW:MEU 데뷔 기념 언급）\n"
        "input:데뷔하고 나서 웬즈들이 생겨서 진짜 행복해요\n"
        "output:出道之後有了웬즈們，真的好幸福"
    ),
}


def _build_base_prompt() -> str:
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

        "[Style]\n"
        "Natural, colloquial Traditional Chinese. Prioritize phrasing from Chinese-speaking streaming communities.\n"
        "Keep tone and emotion. Do not literally translate Korean particles.\n\n"

        "[Preserve As-Is]\n"
        "Do not translate: game names, skill names, streamer IDs, English proper nouns, Korean brand/product names, Korean personal names.\n"
        "Name detection: followed by vocative particles (이/아/야/씨/님), or clearly referring to a specific person in context.\n"
        "이세돌 / 이세계아이돌 = 韓國虛擬偶像團體名稱，直接保留原文 이세돌，禁止翻譯成任何漢字（不是棋士李世乭）。\n"
        "Streaming platforms: 치지직 = CHZZK, SOOP = SOOP — keep as-is.\n"
        "BJ = SOOP/아프리카TV broadcaster title — keep as BJ.\n"
        "치즈 in donation/stream context = CHZZK platform currency — keep as 치즈. NOT food 起司.\n"
        "별풍선 = SOOP donation item — keep as 별풍선.\n\n"

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

        "例 20（訂閱按讚）\n"
        "input: 좋아요랑 구독 부탁해요!\n"
        "output: 麻煩點讚和訂閱！\n\n"

        "例 21（이세돌 = 虛擬偶像組合名，保留原文）\n"
        "input: 오늘 이세돌이 다 모였어요!\n"
        "output: 今天이세돌全員到齊了！\n\n"

        "例 22（이세돌 英雄roleplay語境，仍保留原文）\n"
        "input: 누가 지구를 지키냐고? 이세돌이 지켰어\n"
        "output: 誰守護地球？이세돌守護的！\n"
    )
    profile = _STREAMER_PROFILES.get(cfg.translation.streamer_profile, "")
    if profile and cfg.translation.use_profile:
        base += "\n\n" + profile
    base += "\n\n---\nTranslate the next input. Output the translation only."
    return base

_BASE_PROMPT = _build_base_prompt()


# ---------------------------------------------------------------------------
# Engine abstraction
# ---------------------------------------------------------------------------

class TranslationEngine(ABC):
    """
    Common interface for all translation backends.

    To add a new engine: see the step-by-step guide in config.py (_Translation.engine_chain).
    """

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Short identifier stored in the DB (e.g. 'gemini', 'claude', 'deepl')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model/version string for DB cache keying (e.g. 'gemini-2.5-flash')."""
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """True if the engine initialised successfully and can accept calls."""
        ...

    @abstractmethod
    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        """
        Translate text. Return None on any failure.

        text:          raw source text (Korean)
        system_prompt: evolved prompt — LLM engines use it;
                       direct-translation engines (Google Translate) may ignore it.
        incomplete:    True if the sentence is a fragment.
        history:       recent (ko, zh) pairs; LLM engines prepend as multi-turn messages.
                       Direct-translation engines ignore this.
        """
        ...


class GeminiEngine(TranslationEngine):
    def __init__(self):
        self._client = None
        if not cfg.keys.gemini:
            log.error("GEMINI_API_KEY not set")
            return
        try:
            import google.genai as genai
            from google.genai import types as genai_types
            self._client = genai.Client(
                api_key=cfg.keys.gemini,
                http_options=genai_types.HttpOptions(timeout=_GEMINI_HTTP_TIMEOUT_MS),
            )
            log.info("GeminiEngine ready (model=%s)", cfg.translation.gemini_model)
        except Exception as e:
            log.error("Failed to init Gemini: %s", e)

    @property
    def engine_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return cfg.translation.gemini_model

    @property
    def available(self) -> bool:
        return self._client is not None

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        if self._client is None:
            return None
        try:
            from google.genai import types as genai_types
            contents = []
            for ko, zh in (history or []):
                contents.append(genai_types.Content(
                    role="user", parts=[genai_types.Part(text=f"input: {ko}")]
                ))
                contents.append(genai_types.Content(
                    role="model", parts=[genai_types.Part(text=zh)]
                ))
            contents.append(genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=_build_user_message(text, incomplete))],
            ))
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=cfg.translation.max_tokens,
                temperature=cfg.translation.temperature,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            )
            _t0 = time.monotonic()
            resp = self._client.models.generate_content(
                model=cfg.translation.gemini_model,
                contents=contents,
                config=config,
            )
            log.info("Gemini translate: %.0fms", (time.monotonic() - _t0) * 1000)
            _log_token_usage("Gemini", getattr(resp, "usage_metadata", None))
            result = resp.text.strip()
            log.debug("Gemini: %.30s → %s", text, result)
            return result
        except Exception as e:
            kind = classify_error(e)
            if kind == "auth":
                log.error("Gemini auth error (check GEMINI_API_KEY): %s", e)
            elif kind == "rate_limit":
                log.warning("Gemini rate-limit: %s", e)
            elif kind == "network":
                log.warning("Gemini network error: %s", e)
            else:
                log.error("Gemini error: %s", e)
            return None


class ClaudeEngine(TranslationEngine):
    def __init__(self):
        self._client = None
        if not cfg.keys.anthropic:
            log.error("ANTHROPIC_API_KEY not set")
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=cfg.keys.anthropic)
            log.info("ClaudeEngine ready (model=%s)", cfg.translation.model)
        except Exception as e:
            log.error("Failed to init Anthropic: %s", e)

    @property
    def engine_name(self) -> str:
        return "claude"

    @property
    def model_name(self) -> str:
        return cfg.translation.model

    @property
    def available(self) -> bool:
        return self._client is not None

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        if self._client is None:
            return None
        try:
            _t0 = time.monotonic()
            system_content: dict = {"type": "text", "text": system_prompt}
            if cfg.translation.translation_mode == "live":
                system_content["cache_control"] = {"type": "ephemeral"}
            messages = []
            for ko, zh in (history or []):
                messages.append({"role": "user", "content": f"input: {ko}"})
                messages.append({"role": "assistant", "content": zh})
            messages.append({"role": "user", "content": _build_user_message(text, incomplete)})
            resp = self._client.messages.create(
                model=cfg.translation.model,
                max_tokens=cfg.translation.max_tokens,
                temperature=cfg.translation.temperature,
                system=[system_content],
                messages=messages,
                timeout=5.0,
            )
            log.info("Claude translate: %.0fms", (time.monotonic() - _t0) * 1000)
            _log_token_usage("Claude", getattr(resp, "usage", None))
            result = resp.content[0].text.strip()
            log.debug("Claude: %.30s → %s", text, result)
            return result
        except Exception as e:
            kind = classify_error(e)
            if kind == "auth":
                log.error("Claude auth error (check ANTHROPIC_API_KEY): %s", e)
            elif kind == "rate_limit":
                log.warning("Claude rate-limit: %s", e)
            elif kind == "network":
                log.warning("Claude network error: %s", e)
            else:
                log.error("Claude error: %s", e)
            return None


class GoogleTranslateEngine(TranslationEngine):
    """Google Cloud Translation API v2 (Basic). No LLM — ignores system_prompt."""

    _URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self):
        self._api_key = cfg.keys.google_translate
        self._target_lang = cfg.translation.google_translate_lang
        if not self._api_key:
            log.error("GOOGLE_TRANSLATE_API_KEY not set")

    @property
    def engine_name(self) -> str:
        return "google_translate"

    @property
    def model_name(self) -> str:
        return "google-translate-v2"

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def translate(self, text: str, _system_prompt: str, _incomplete: bool,
                  _history: list[tuple[str, str]] | None = None) -> str | None:  # pyright: ignore[reportUnusedParameter]
        if not self._api_key:
            return None
        try:
            import urllib.request
            import urllib.parse
            import json as _json
            payload = _json.dumps({
                "q": text,
                "source": "ko",
                "target": self._target_lang,
                "format": "text",
            }).encode()
            url = f"{self._URL}?key={urllib.parse.quote(self._api_key, safe='')}"
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            _t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=5) as r:
                data = _json.loads(r.read())
            result = data["data"]["translations"][0]["translatedText"].strip()
            log.info("GoogleTranslate translate: %.0fms", (time.monotonic() - _t0) * 1000)
            log.debug("GoogleTranslate: %.30s → %s", text, result)
            return result
        except Exception as e:
            safe = str(e).replace(self._api_key, "***") if self._api_key else str(e)
            kind = classify_error(e)
            if kind == "auth":
                log.error("GoogleTranslate auth error (check GOOGLE_TRANSLATE_API_KEY): %s", safe)
            elif kind == "rate_limit":
                log.warning("GoogleTranslate rate-limit: %s", safe)
            elif kind == "network":
                log.warning("GoogleTranslate network error: %s", safe)
            else:
                log.error("GoogleTranslate error: %s", safe)
            return None


class OllamaEngine(TranslationEngine):
    """Ollama local model via OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self):
        self._base_url = cfg.ollama.base_url.rstrip("/")
        self._model = cfg.ollama.model
        self._timeout = cfg.ollama.timeout
        log.info("OllamaEngine ready (model=%s, base_url=%s)", self._model, self._base_url)

    @property
    def engine_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return True

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in (history or []):
            messages.append({"role": "user", "content": f"input: {ko}"})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": _build_user_message(text, incomplete)})

        payload = _json.dumps({
            "model": self._model,
            "messages": messages,
            "stream": False,
            "temperature": cfg.translation.temperature,
            "max_tokens": cfg.translation.max_tokens,
        }).encode()

        url = f"{self._base_url}/v1/chat/completions"
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            _t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = _json.loads(r.read())
            log.info("Ollama translate: %.0fms", (time.monotonic() - _t0) * 1000)
            usage = data.get("usage", {})
            log.info("Ollama tokens | prompt=%s output=%s",
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
            result = data["choices"][0]["message"]["content"].strip()
            log.debug("Ollama: %.30s → %s", text, result)
            return result
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log.error("模型 %r 不存在，請先執行 `ollama pull %s`", self._model, self._model)
            else:
                log.error("Ollama HTTP %d: %s", e.code, e)
            return None
        except urllib.error.URLError as e:
            reason = str(e).lower()
            # WinError 10061 = connection refused on Windows
            if "refused" in reason or "10061" in reason or "connect" in reason:
                log.error("Ollama 未啟動或 base_url 設定錯誤 (%s) — 請先執行 `ollama serve`", self._base_url)
            else:
                log.error("Ollama network error: %s", e)
            return None
        except Exception as e:
            log.error("Ollama error: %s", e)
            return None


class NvidiaEngine(TranslationEngine):
    """NVIDIA NIM hosted models via OpenAI-compatible endpoint."""

    _BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

    def __init__(self):
        self._api_key = cfg.keys.nvidia
        self._model = cfg.nvidia.model
        self._timeout = cfg.nvidia.timeout
        _m = self._model.lower()
        self._is_qwen3    = "qwen3" in _m or "qwen-3" in _m
        self._strip_think = self._is_qwen3 or any(x in _m for x in ("deepseek-v4", "deepseek-r1", "deepseek-v3"))
        if not self._api_key:
            log.error("NVIDIA_API_KEY not set")
        else:
            log.info("NvidiaEngine ready (model=%s, qwen3=%s, strip_think=%s)",
                     self._model, self._is_qwen3, self._strip_think)

    @property
    def engine_name(self) -> str:
        return "nvidia"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        if not self._api_key:
            return None
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in (history or []):
            messages.append({"role": "user", "content": f"input: {ko}"})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": _build_user_message(text, incomplete)})

        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": cfg.translation.temperature,
            "max_tokens": cfg.translation.max_tokens,
        }
        if self._is_qwen3:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        payload = _json.dumps(body).encode()

        req = urllib.request.Request(
            self._BASE_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            _t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = _json.loads(r.read())
            log.info("Nvidia translate: %.0fms", (time.monotonic() - _t0) * 1000)
            usage = data.get("usage", {})
            log.info("Nvidia tokens | prompt=%s output=%s",
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if self._strip_think:
                import re as _re
                content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
            log.debug("Nvidia: %.30s → %s", text, content)
            return content or None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            if e.code == 401:
                log.error("Nvidia auth error — NVIDIA_API_KEY 無效或已過期")
            elif e.code == 404:
                log.error("Nvidia 模型 %r 不存在 — 請確認 build.nvidia.com 上的模型名稱", self._model)
            elif e.code == 429:
                log.warning("Nvidia rate-limit (429) — 請求過於頻繁，略過此句")
            else:
                log.error("Nvidia HTTP %d: %s", e.code, body or e)
            return None
        except urllib.error.URLError as e:
            log.error("Nvidia network error: %s", e)
            return None
        except Exception as e:
            log.error("Nvidia error: %s", e)
            return None


def _make_engine(name: str) -> "TranslationEngine | None":
    """Instantiate an engine by name. Returns None if unavailable or unknown."""
    if name == "gemini":
        e = GeminiEngine()
        return e if e.available else None
    if name == "claude":
        e = ClaudeEngine()
        return e if e.available else None
    if name == "google_translate":
        e = GoogleTranslateEngine()
        return e if e.available else None
    if name == "ollama":
        return OllamaEngine()
    if name == "nvidia":
        e = NvidiaEngine()
        return e if e.available else None
    log.warning("Unknown engine %r in engine_chain — skipping", name)
    return None


def _build_engine_chain() -> "list[TranslationEngine]":
    """Build an ordered list of available engines.

    Picks cfg.live_engine or cfg.clip_engine based on current translation_mode.
    "ollama"/"nvidia" bypass engine_chain entirely — no fallback.
    "anthropic" (default) uses engine_chain with ordered fallback.
    """
    mode = cfg.translation.translation_mode
    engine_name = cfg.clip_engine if mode == "clip" else cfg.live_engine
    log.info("Engine selection: mode=%s → engine=%s", mode, engine_name)
    if engine_name == "ollama":
        return [OllamaEngine()]
    if engine_name == "nvidia":
        e = NvidiaEngine()
        if not e.available:
            log.error("NvidiaEngine unavailable — check NVIDIA_API_KEY")
            return []
        fallbacks = [fb for name in cfg.translation.engine_chain
                     if (fb := _make_engine(name)) is not None]
        if fallbacks:
            log.info("NvidiaEngine ready with fallback chain: %s",
                     [fb.engine_name for fb in fallbacks])
        return [e] + fallbacks
    engines = [e for name in cfg.translation.engine_chain
               if (e := _make_engine(name)) is not None]
    if not engines:
        log.error("No translation engines available — all engines failed to initialise")
    return engines


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class Translator:
    def __init__(self):
        self._evolver = PromptEvolver()
        self._cache: OrderedDict = OrderedDict()
        self._engines: list[TranslationEngine] = _build_engine_chain()
        self._active_idx = 0
        self._probe_counter = 0
        self._recent: deque[tuple[str, str]] = deque(maxlen=cfg.translation.context_window)
        self._last_input: str = ""

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        text = text.strip()
        if not text:
            return None

        # Suppress consecutive identical inputs (VAD double-cut of the same segment)
        if text == self._last_input:
            log.debug("Duplicate input suppressed: %.40s", text)
            return None
        self._last_input = text

        if len(text) < _MIN_TRANSLATE_CHARS:
            log.debug("Skipping: too short (%d chars)", len(text))
            return None

        # B: exact slang match — zero API tokens
        slang_result = cfg.translation.slang.get(text)
        if slang_result:
            log.debug("Slang hit: %s → %s", text, slang_result)
            self._evolver.record(text, slang_result)
            _write_history(text, slang_result)
            if not incomplete:
                self._recent.append((text, slang_result))
            return slang_result

        system_prompt = self._evolver.build_system_prompt(_BASE_PROMPT)
        prompt_ver = hashlib.md5(system_prompt.encode()).hexdigest()[:8]

        # A: memory cache hit — zero API tokens
        cached = self._cache_lookup(text, incomplete, prompt_ver)
        if cached:
            log.debug("Cache hit: %s", text[:20])
            if not incomplete:
                self._recent.append((text, cached))
            return cached

        # C: DB lookup — complete sentences only
        if not incomplete and self._engines:
            db_result = self._db_lookup(text, self._engines[self._active_idx], prompt_ver)
            if db_result:
                self._cache_store(text, incomplete, db_result, prompt_ver)
                self._recent.append((text, db_result))
                return db_result

        history = list(self._recent)
        result = self._call_with_fallback(text, system_prompt, incomplete, history)
        if result:
            self._cache_store(text, incomplete, result, prompt_ver)
            self._evolver.record(text, result)
            _write_history(text, result)
            if not incomplete:
                self._recent.append((text, result))
                self._db_store(text, result, self._engines[self._active_idx], prompt_ver)
        else:
            # API failure — allow next identical input to retry rather than staying suppressed
            self._last_input = ""
        return result

    def _call_with_fallback(self, text: str, system_prompt: str, incomplete: bool,
                            history: list[tuple[str, str]] | None = None) -> str | None:
        if not self._engines:
            return None

        if self._active_idx > 0:
            self._probe_counter += 1
            if self._probe_counter >= _FALLBACK_PROBE_EVERY:
                self._probe_counter = 0
                probe = self._engines[0].translate(text, system_prompt, incomplete, history)
                if probe and not _looks_untranslated(probe, text):
                    log.info("Primary engine %s recovered — switching back",
                             self._engines[0].engine_name)
                    self._active_idx = 0
                    return probe
                log.debug("Primary probe failed, staying on %s",
                          self._engines[self._active_idx].engine_name)

        for i in range(self._active_idx, len(self._engines)):
            result = self._engines[i].translate(text, system_prompt, incomplete, history)
            if result and not _looks_untranslated(result, text):
                if i > self._active_idx:
                    log.warning("Engine %s failed — switching to %s",
                                self._engines[self._active_idx].engine_name,
                                self._engines[i].engine_name)
                    self._active_idx = i
                    self._probe_counter = 0
                return result

        log.error("All engines failed for: %.40s", text)
        return None

    def _get_prompt_version_hash(self) -> str:
        system_prompt = self._evolver.build_system_prompt(_BASE_PROMPT)
        return hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    def _cache_store(self, text: str, incomplete: bool, value: str, prompt_ver: str) -> None:
        key = (text, incomplete, prompt_ver)
        if len(self._cache) >= _CACHE_MAX_SIZE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = value

    def _cache_lookup(self, text: str, incomplete: bool, prompt_ver: str) -> str | None:
        key = (text, incomplete, prompt_ver)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _db_lookup(self, text: str, engine: TranslationEngine, prompt_ver: str) -> str | None:
        return _get_db().lookup(
            text, cfg.translation.target_lang,
            engine.engine_name, engine.model_name, prompt_ver,
        )

    def _db_store(self, text: str, result: str, engine: TranslationEngine, prompt_ver: str) -> None:
        _get_db().store(
            text, result, cfg.translation.target_lang,
            engine.engine_name, engine.model_name, prompt_ver,
        )


_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window


def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        translator = Translator()
        last_result = ""
        last_result_time = 0.0
        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                stop_event.wait(timeout=0.2)
                continue
            try:
                item = sentence_queue.get(timeout=1)
            except queue.Empty:
                continue

            text = item["text"]
            incomplete = item.get("incomplete", False)
            result = translator.translate(text, incomplete)
            if result:
                now = time.monotonic()
                if result == last_result and (now - last_result_time) < _DEDUP_SUBTITLE_SEC:
                    log.debug("Suppressing duplicate subtitle: %s", result[:30])
                    continue
                last_result = result
                last_result_time = now
                drained = drain_put(subtitle_queue, result)
                if drained:
                    log.warning("subtitle_queue backlog cleared (%d), keeping latest", drained)

        log.info("Translator stopped")

    t = threading.Thread(target=run, name="Translator", daemon=True)
    t.start()
    return t


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
