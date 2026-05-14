from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptionEvent:
    text: str
    engine: str
    profile_id: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True)
class SentenceEvent:
    text: str
    incomplete: bool = False
    profile_id: str = ""
    stt_engine: str = ""
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


def transcription_text(item: str | TranscriptionEvent) -> str:
    if isinstance(item, TranscriptionEvent):
        return item.text
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item)


def transcription_to_sentence(
    text: str,
    incomplete: bool,
    source: TranscriptionEvent | None = None,
) -> SentenceEvent:
    if source is None:
        return SentenceEvent(text=text, incomplete=incomplete)
    return SentenceEvent(
        text=text,
        incomplete=incomplete,
        profile_id=source.profile_id,
        stt_engine=source.engine,
        avg_logprob=source.avg_logprob,
        no_speech_prob=source.no_speech_prob,
    )


def sentence_text(item: SentenceEvent | dict | str) -> str:
    if isinstance(item, SentenceEvent):
        return item.text
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item)


def sentence_incomplete(item: SentenceEvent | dict | str) -> bool:
    if isinstance(item, SentenceEvent):
        return item.incomplete
    if isinstance(item, dict):
        return bool(item.get("incomplete", False))
    return False


def sentence_metadata(item: SentenceEvent | dict | str) -> dict:
    if isinstance(item, SentenceEvent):
        return {
            "profile_id": item.profile_id,
            "stt_engine": item.stt_engine,
            "avg_logprob": item.avg_logprob,
            "no_speech_prob": item.no_speech_prob,
        }
    if isinstance(item, dict):
        return {
            "profile_id": item.get("profile_id", ""),
            "stt_engine": item.get("stt_engine", ""),
            "avg_logprob": item.get("avg_logprob"),
            "no_speech_prob": item.get("no_speech_prob"),
        }
    return {
        "profile_id": "",
        "stt_engine": "",
        "avg_logprob": None,
        "no_speech_prob": None,
    }
