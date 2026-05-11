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


_STREAMER_PROFILES_QWEN: dict[str, str] = {
    "stellive_hina": (
        "【시라유키 히나 (Qwen 專化)】\n"
        "Streamer: 시라유키 히나 (Stellive). Fan name: 해둥이 (海洞). Signature game: Minecraft (포포 서버), Sekiro, Valorant.\n"
        "Personality: 夜貓直播主，多巴胺充沛，撒嬌，玩遊戲常比較卡關。\n\n"

        "1. 粉絲互動（해둥이）\n"
        "input:해둥이들 오늘도 와줘서 고마워! 기니까 꽉 안아줄게\n"
        "output:해둥이們今天也來了謝謝，既然都來了就給你們一個大擁抱\n\n"

        "2. 深夜開台 (新的一天，同時維持夜貓身份)\n"
        "input:지금 새벽 3시인데 해둥이들 아직 안 잤어?\n"
        "output:現在早上三點了，해둥이們還沒睡嗎？\n\n"

        "3. 도파민 모드 (多巴胺直播，快樂能量)\n"
        "input:아 이거 봐봐 도파민 넘친다 진짜ㅋㅋ 완전 쌀 수 없어\n"
        "output:啊你看我的多巴胺都爆炸了真的哈哈完全停不下來\n\n"

        "4. 말벌 (丟下一切追稀有寶可夢，Pokémon焦點轉移)\n"
        "input:어? 저기 희귀 포켓몬이! 다른 거 다 잊어 그것만 봐\n"
        "output:咦？那邊有稀有寶可夢！其他都別管只看那個\n\n"

        "5. 카바 사기 自嘲 (封面詐欺梗)\n"
        "input:내가 커버 사기 맞죠? 처음엔 조용한 줄 알지?\n"
        "output:我就是封面詐欺對吧，一開始以為我文靜嗎？\n\n"

        "6. Sekiro 挫折反應\n"
        "input:세키로 진짜... 또 죽었어 이거 어떻게 이래ㅠㅠ\n"
        "output:隻狼真的……又死了，這玩法到底是怎樣ㅠㅠ"
    ),

    "isegye_lilpa": (
        "【릴파 / 이세계아이돌 (Qwen 專化)】\n"
        "Streamer: 릴파 (이세계아이돌 member). Fan name: 박쥐단 (蝙蝠團) / 이파리 (이세돌粉絲). Signature game: LoL, Valorant, Minecraft.\n"
        "Personality: 開朗愛玩，나비다梗，징버거梗常出現，與성원互動活躍。\n\n"

        "1. 粉絲問候（박쥐단）\n"
        "input:박쥐단들 오늘도 잘 놀고 있어? 나는 지금 엄청 즐거운데 함께해줘서 고마워\n"
        "output:박쥐단們今天也玩得開心嗎？我現在超開心謝謝你們陪著我\n\n"

        "2. 나비다 Meme (看到蝴蝶分心走神)\n"
        "input:어 잠깐만... 나비다! 이 방송 2분만 멈췄다 올게 꼭 있어\n"
        "output:欸等等……有蝴蝶！這直播暫停2分鐘，一定要來啦\n\n"

        "3. 징버거 梗 (짐버거)\n"
        "input:당신은 이 게임을 이겨야 합니다. 아 미안해 징버거 생각났어\n"
        "output:你這盤要贏。啊不好意思想到징버거去了\n\n"

        "4. LoL 排名 (Valorant 배열 焦慮)\n"
        "input:아 이겨야 돼 다이아 떨어질 수 없어 이파리들 기원해줄래\n"
        "output:啊一定要贏掉不能從鑽石掉下來，이파리們替我祈禱\n\n"

        "5. Minecraft 建築 (공사 중 相對樂觀)\n"
        "input:여기다 진짜 멋진 집 지을 거예요 이파리들 봐줄 거죠\n"
        "output:在這裡要蓋個超酷的房子，이파리們要看喔\n\n"

        "6. 成員互動 (팀 컬래버)\n"
        "input:아이네 언니 빨리 와! 우리 이 미션 같이 깼으면 좋겠어\n"
        "output:아이네姐姐快來！我好想跟妳一起破這關"
    ),

    "hades_chxxnnx": (
        "【챈나 / HADES (Qwen 專化)】\n"
        "Streamer: 챈나 (HADES group). Members: 솜펀치, 연초록, 큐마, 싱귤. No official fan name.\n"
        "Personality: HADES 막內，撒嬌，常跟成員互動，有時遊戲卡關會求助。\n\n"

        "1. 成員召喚（솜펀치 호출）\n"
        "input:솜펀치 언니 나 혼자 심심한데 와줄 수 있어? 제발이야\n"
        "output:솜펀치姐姐我一個人好無聊，能來陪我嗎，拜託了\n\n"

        "2. 멤버 응원（큐마 칭찬）\n"
        "input:큐마 이 부분 너무 잘했어 너라서 진짜 다행이야\n"
        "output:큐마這段超厲害，幸好有妳\n\n"

        "3. 연초록 팬 梗 (連初綠沒反應)\n"
        "input:연초록이 또 연락이 안 되네 진짜 이 사람 원래 이래\n"
        "output:연초록又沒反應了，這人總是這樣\n\n"

        "4. 싱귤 노래 응원\n"
        "input:싱귤이 노래하는 거 진짜 듣고 싶은데 언제 할 거야\n"
        "output:好想聽싱귤唱歌，什麼時候唱啦\n\n"

        "5. 게임 中 도움 요청\n"
        "input:어 이거 어떻게 하는 거야 나 자꾸 죽네 도와줄 사람\n"
        "output:欸這怎麼玩啊我一直死掉有人能幫我嗎\n\n"

        "6. HADES 활동 감사\n"
        "input:하데스 다함께 방송할 수 있어서 진짜 행복해 다들 좋아\n"
        "output:能和HADES一起直播真的很開心，大家都超好"
    ),

    "mwmeu": (
        "【MW:MEU (Qwen 專化)】\n"
        "Streamer: MW:MEU group. Members: 지한 (최고령언니), 이비, 수아, 리츠, 초은. Fan name: WENs (웬즈).\n"
        "Personality: 5人組グループ，成員彼此互相照應，粉絲 WENs 忠誠度高。\n\n"

        "1. 粉絲問候（웬즈）\n"
        "input:웬즈들 오늘도 우리랑 시간 함께해줘서 정말 감사해 항상 사랑해\n"
        "output:웬즈們今天也跟我們一起真的超謝謝你們，永遠愛你們\n\n"

        "2. 지한 멤버 호출（最高령언니的角色）\n"
        "input:지한이 언니 나 이 부분 모르는데 좀 가르쳐줄래 제발\n"
        "output:지한姐姐我不懂這裡，能教我嗎，拜託\n\n"

        "3. 이비 응원\n"
        "input:이비 이 노래 너무 좋다 목소리 진짜 천상의 목소리 같아\n"
        "output:이비這首歌超好聽，妳的聲音真的像仙籟\n\n"

        "4. 수아 칭찬（맑은 음성）\n"
        "input:수아 목소리 듣고 있으면 진짜 기분이 좋아져 계속해줄래\n"
        "output:聽수아唱歌心情會變好，一直唱好嗎\n\n"

        "5. 리츠·초은 互動\n"
        "input:리츠랑 초은이가 또 뭔가 하고 있다 진짜 이 두 사람 항상 재밌어\n"
        "output:리츠和초은又在搞什麼，這兩人真的超好玩\n\n"

        "6. WENs 팬덤 感謝\n"
        "input:웬즈들이 있으니까 우리도 계속 열심히 할 수 있어 고마워 사랑해\n"
        "output:有웬즈們的支持我們才能繼續加油，謝謝你們，愛你們"
    ),
}


def _is_qwen_model() -> bool:
    """检查当前后端是否为 Qwen 模型"""
    if cfg.live_engine == "nvidia":
        model = cfg.nvidia.model.lower()
        return "qwen" in model
    return False


def _build_base_prompt() -> str:
    """生成通用 system prompt（兼容所有引擎）"""
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

        "例 20（訂閱按讚）\n"
        "input: 좋아요랑 구독 부탁해요!\n"
        "output: 麻煩點讚和訂閱！\n\n"

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
    profile = _STREAMER_PROFILES.get(cfg.translation.streamer_profile, "")
    if profile and cfg.translation.use_profile:
        base += "\n\n" + profile
    base += "\n\n---\nTranslate the next input. Output the translation only."
    return base


def _build_qwen_optimized_prompt() -> str:
    """Qwen 3.5-122B 专属优化 prompt - 充分利用强大能力，不限 token"""
    slang_lines = "\n".join(f"  {k} → {v}" for k, v in cfg.translation.slang.items())
    slang_part = (
        f"\n【常用詞彙表】（優先使用以下對照）\n{slang_lines}"
        if slang_lines else ""
    )

    stt_section = (
        "[STT 原始字符串錯誤 - Qwen 必讀]\n"
        "輸入來自語音識別(STT)，是**原始、未經編輯**的文本。\n"
        "STT 常見問題：\n"
        "· 幻覺詞彙 - 捏造不存在的詞（如'비맥스로''제트로''내미쉬'）\n"
        "· 商業混入 - 廣告、網站推廣被當成語音（'사이트 들어가보세요''약사님께'）\n"
        "· 語義破碎 - 多個無關詞拼接，完全無邏輯\n"
        "· 重複無義 - 同一詞重複多次無新信息（'21개월...21개월'）\n"
        "· 外語雜訊 - 外語詞混入無上下文（'gesch musste''すごい'）\n\n"
        
        "【致命特徵 - 直接返回空字串】\n"
        "· 多個無關韓文詞彙拼接無邏輯（'풀리지 않는 피로의 비맥스로...제트로' ← 完全亂序）\n"
        "· 同一詞重複≥2次且占比>50%（'21개월...21개월' ← 單純重複）\n"
        "· 商業廣告/網站促銷混入（'설명은 약사님께 풀도 지금 사이트 들어가보세요' ← STT 幻覺）\n"
        "· 外語+韓文無邏輯混雜（'비맥스 제트' ← 無實詞，瞎編）\n"
        "→ 這些情況直接返回**空字串**，不要試圖翻譯\n\n"
        
        "【錯誤特徵 - 保守翻譯或簡化】\n"
        "· 生僻韓文+語義混亂（'고지야'單獨出現、'아이고'無上下文）→保留韓文或簡化\n"
        "· 不完整句子（明顯缺語法標記）→盡量翻譯，勿補充\n"
        "· 外語片段無韓文/中文上下文（'gesch musste'、'すごい'）→直接省略\n\n"
    )

    base = (
        f"你是專業韓文→繁體中文直播字幕翻譯器。目標語言：{cfg.translation.target_lang}。\n"
        "你的任務是精準、自然地將韓文直播內容翻譯成繁體中文字幕，保留原意和情感。\n\n"
        
        "⚠️ 重要：輸入是語音識別(STT)的**原始字符串**，可能包含大量錯誤、雜訊、幻覺詞彙。\n"
        "你的首要責任是**識別和過濾垃圾**，而非試圖補全或推測原意。\n\n"

        "[核心翻譯原則]\n"
        "1. 只輸出翻譯，無任何前置詞、標籤、引號或後設說明\n"
        "2. 無法翻譯的詞彙直接省略，勿生硬翻譯或猜測\n"
        "3. 輸入為純雜訊或無意義→輸出空字串（無任何解釋）\n"
        "4. 繁體中文（繁體）專用，嚴禁簡體中文、日文或其他語言\n"
        "5. 保留原文的語氣、強度和情感，勿過度正式化或削弱\n"
        "6. ⚠️ **禁止補充、推論、擴展原文沒有的內容**\n"
        "   因為輸入是 STT 原始字符串，補充只會讓幻覺更嚴重\n"
        "   例如輸入'비맥스로...'是 STT 垃圾，不要補充'非最大級別''詳細說明'等\n"
        "   例如輸入'풀도 사이트 들어가보세요'包含廣告，不要補充'諮詢藥師'\n"
        "   無法理解的 STT 垃圾→返回空字串，不要嘗試補全\n"
        "7. ⚠️ **信任 STT 錯誤檢測的前期過濾**\n"
        "   前置步驟已經過濾掉明顯的垃圾，但仍可能有邊界情況\n"
        "   若句子語義完全破碎→返回空，勿嘗試修復\n\n"

        + stt_section +

        "[翻譯優先級 - 基於 STT 信心度]\n"
        "【A 級 - 絕對保留原文】\n"
        "· 遊戲/角色名稱：Valorant、Chikorita、Pidgeot、이세돌、VVIP\n"
        "· 直播平台：치지직(CHZZK)、SOOP、아프리카TV (保留英文或原名)\n"
        "· 虛擬偶像團體：이세돌、이세계아이돌→保留原文（非李世乭）\n"
        "· 主播專屬詞：기부자、후원자、알바、BJ（主播title）\n"
        "· 不明物品/品牌：파이리빵、내미쉬→保留韓文（勿猜測）\n"
        "· 韓文人名+敬語粒子(이/아/야/씨/님)：민준아→民俊、세율이→世律\n"
        "· ⚠️ **STT 幻覺詞不保留** - 如'비맥스로'、'제트로'這種無意義詞→直接省略\n\n"

        "【B 級 - 智慧翻譯但保留強度】\n"
        "· 粗俗/俚語：드럽게→爛透了/糟到不行（保留負面強度，非污穢之意）\n"
        "· 語氣詞：막→就是/每天/一直（根據語境，可省略）、맨날→每天\n"
        "· 感嘆詞：헐→天啊、와→哇、어머→天哪\n"
        "· 語尾助詞：-네→欸/哇、-ㄹ게→我會、-아/어 죽겠다→死我了/超級\n"
        "· 直播術語：방송→直播、뱅종/뱅송→下播/直播、방종→結束直播\n\n"

        "【C 級 - 中文化翻譯】\n"
        "· 通用表達：진짜→真的、완전→完全、뭔가→有點\n"
        "· 反應詞：대박→太狂了、ㅋㅋ→哈哈、억까→被針對\n"
        "· 讚賞：꿀잼→超好玩、잘한다→厲害\n\n"

        "[特殊規則]\n"
        "· 치즈：在打賞/直播平台文脈 = CHZZK 貨幣→保留「치즈」（非食物起司）\n"
        "· 別풍선：SOOP 打賞道具→保留「별풍선」\n"
        "· 不完整句子：盡量翻譯，勿補充缺失內容\n"
        "· 重複數字+邏輯不通：簡化或去重（如'21개월...21개월'→'21 個月'）\n"
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
        "例10 - KO: 좋아요랑 구독 부탁해요! | ZH: 麻煩點讚和訂閱！\n"
        "例11 - KO: 13개월 구독 고맙습니다 | ZH: 13 個月訂閱，感謝！\n\n"

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

        "【STT 幻覺/垃圾 - 返回空或大幅簡化】\n"
        "例26 - KO: 풀리지 않는 피로의 비맥스로 피로는 제대로 비맥스 제트로 설명은 약사님께 풀도 지금 사이트 들어가보세요 말 안됨\n"
        "        | ZH: (空字串) ← 完全垃圾 STT，勿試圖補全或推測\n"
        "例27 - KO: 아 인도는 그 뒤에서 생진 피팅 막 기왕이던데 밖에서는\n"
        "        | ZH: (空字串或簡化為) 啊，印度那邊...\n"
        "        ← STT 破碎，無法連貫翻譯\n\n"

        "【保留原文/不翻譯】\n"
        "例27 - KO: 오늘 이세돌이 다 모였어요! | ZH: 今天이세돌全員到齊了！\n"
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
    
    # Qwen 専用 prompt 使用 Qwen 専用档案
    profile = _STREAMER_PROFILES_QWEN.get(cfg.translation.streamer_profile, "")
    if profile and cfg.translation.use_profile:
        base += "\n\n" + profile
    base += "\n\n---\n只輸出翻譯。無任何其他文字。"
    return base


_BASE_PROMPT = _build_base_prompt()
_QWEN_PROMPT = _build_qwen_optimized_prompt()  # Qwen 专属优化版


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

    @staticmethod
    def _is_stt_garbage(text: str) -> bool:
        """
        检测输入是否为 STT 垃圾。如果是，返回 True，应该被过滤掉。
        
        检测规则：
        1. 重复词汇（同一词≥2次无新信息）
        2. 无逻辑混杂（多语言+韩文混乱）
        3. 广告/网站指令混入
        """
        # 分词（简单分割）
        words = text.split()
        if len(words) < 3:
            return False
        
        # 规则1: 同一词重复≥2次且占比>60%
        word_count = {}
        for w in words:
            word_count[w] = word_count.get(w, 0) + 1
        
        repeat_ratio = max(word_count.values()) / len(words) if words else 0
        if repeat_ratio > 0.6:
            log.debug("STT garbage detected: excessive repetition (ratio=%.2f) in '%s'", repeat_ratio, text[:50])
            return True
        
        # 规则2: 混入商业/网站指令关键词（사이트, 약사님, 지금 들어가보세요 等）
        garbage_keywords = ['사이트', '들어가보세요', '약사님께', '추천', '광고', '구매', '클릭', '방문']
        if any(kw in text for kw in garbage_keywords) and '?' not in text and '!' not in text:
            # 如果有这些关键词但缺乏自然语气标记，可能是 STT 幻觉
            log.debug("STT garbage detected: commercial keywords in '%s'", text[:50])
            return True
        
        # 规则3: 外语词+韩文混乱（如'비맥스로...제트로'无意义外语）
        import re
        han_pattern = re.compile(r'[가-힣]')
        eng_pattern = re.compile(r'[a-zA-Z]{3,}')  # 3+ 字母视为英文
        
        has_korean = bool(han_pattern.search(text))
        has_english = bool(eng_pattern.search(text))
        
        if has_korean and has_english:
            # 英文部分都是无意义的（如비맥스, 제트 这样的瞎编）
            eng_words = eng_pattern.findall(text)
            if all(len(w) < 4 for w in eng_words):  # 全是短无意义词
                log.debug("STT garbage detected: random english mixed with korean in '%s'", text[:50])
                return True
        
        return False

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

        # STT 垃圾检测 - 直接过滤掉明显的垃圾输入
        if self._is_stt_garbage(text):
            log.debug("Filtering STT garbage: %.40s", text)
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

        # 根据当前模型选择对应的 prompt
        is_qwen = _is_qwen_model()
        base_prompt = _QWEN_PROMPT if is_qwen else _BASE_PROMPT
        system_prompt = self._evolver.build_system_prompt(base_prompt)
        prompt_ver = hashlib.md5(system_prompt.encode()).hexdigest()[:8]
        if is_qwen and not hasattr(self, '_qwen_log_once'):
            log.info("Using Qwen-optimized system prompt (shorter, more direct)")
            self._qwen_log_once = True

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
