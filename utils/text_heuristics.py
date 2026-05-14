import re


SENSEVOICE_NOISE_TAGS = {
    "<|BGM|>",
    "<|Applause|>",
    "<|Laughter|>",
    "<|Cry|>",
    "<|Sneeze|>",
    "<|Breath|>",
    "<|Cough|>",
}

SENSEVOICE_TAG_RE = re.compile(r"<\|[^|]*\|>")

SENTENCE_COMPLETE_ENDINGS = tuple(
    sorted(
        [
            "겠어",
            "겠다",
            "구나",
            "ㅋㅋ",
            "ㅎㅎ",
            "ㅠㅠ",
            "죠",
            "요",
            "다",
            "어",
            "아",
            "네",
            "예",
            "야",
            "지",
            "군",
            "ㅠ",
            "!",
            "?",
            "~",
        ],
        key=len,
        reverse=True,
    )
)

SENTENCE_INCOMPLETE_ENDINGS = tuple(
    sorted(
        [
            "는데",
            "니까",
            "거든",
            "잖아",
            "하고",
            "이고",
            "이서",
            "아서",
            "어서",
            "으로",
            "에서",
            "고",
            "서",
            "면",
        ],
        key=len,
        reverse=True,
    )
)

STT_GARBAGE_KEYWORDS = (
    "사이트",
    "들어가보세요",
    "약사님께",
    "추천",
    "광고",
    "구매",
    "클릭",
    "방문",
)

STT_FRAGMENTED_MARKERS = (
    "자꾸",
    "막",
    "이렇게",
    "뭔",
    "그냥",
    "약간",
)

KOREAN_CHAR_RE = re.compile(r"[가-힣]")
ENGLISH_WORD_RE = re.compile(r"[a-zA-Z]{3,}")
DIGIT_RE = re.compile(r"\d+")
