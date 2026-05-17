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

# Historical full keyword list kept for compatibility/imports. The translation
# policy uses STT_GARBAGE_STRONG_KEYWORDS for single-keyword rejection so weak
# terms like "추천" / "구매" do not suppress normal speech by themselves.
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

STT_GARBAGE_STRONG_KEYWORDS = (
    "사이트",
    "들어가보세요",
    "약사님께",
    "광고",
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

# Task #8 — STT template hallucination guard.
# These are YouTube / 自媒體 boilerplate that Groq/Whisper fabricate on
# low-confidence audio. They are NOT translator hallucinations.
#
# HARD: caption/ad disclaimers a live streamer would essentially never say
# out loud. Substring presence alone is enough to treat as garbage.
STT_TEMPLATE_HARD_PHRASES = (
    "자막 제공",
    "한글자막 제공",  # redundant with "자막 제공" but listed for clarity
    "광고를 포함하고 있습니다",
    "카카오톡 플러스친구",
    "kakaotalk 플러스친구",
    "홈페이지에서 확인하실 수 있습니다",
)

# CONDITIONAL: canonical outro CTAs. A streamer CAN genuinely say short
# thanks ("구독 감사합니다"), so these full phrases only count as garbage
# when they DOMINATE the utterance or repeat — see
# TranslationPolicy.is_stt_template_garbage for the exact rule.
STT_TEMPLATE_CONDITIONAL_PHRASES = (
    "시청해주셔서 감사합니다",
    "구독과 좋아요는 저에게 큰 힘이 됩니다",
    "구독과 좋아요는 저에게 아주 큰 힘이 됩니다",
)

# Task #9 — boundary sanitizer. Keep this separate from the conditional guard
# list so stripping can diverge from rejection rules later if runtime data
# shows different false-positive behavior.
STT_TEMPLATE_STRIP_PHRASES = STT_TEMPLATE_CONDITIONAL_PHRASES

# Significant chars = drop whitespace / punctuation / ellipsis so length
# ratios are not skewed by trailing "." / "!" / "...".
STT_INSIGNIFICANT_RE = re.compile(r"[\s.,!?~…·、。！？]+")

KOREAN_CHAR_RE = re.compile(r"[가-힣]")
ENGLISH_WORD_RE = re.compile(r"[a-zA-Z]{3,}")
DIGIT_RE = re.compile(r"\d+")
