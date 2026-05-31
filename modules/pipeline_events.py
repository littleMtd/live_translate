from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptionEvent:
    text: str
    engine: str
    profile_id: str
    # Correlation id minted once per STT transcription so audio→stt→sentence→
    # translation events for the same utterance can be joined in the logs.
    utterance_id: str = ""
    # Duration of the audio chunk this transcription came from; summed by the
    # sentence buffer to report a sentence's total audio span.
    audio_seconds: float = 0.0
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True)
class SentenceEvent:
    text: str
    incomplete: bool = False
    profile_id: str = ""
    stt_engine: str = ""
    # Carried through from the source TranscriptionEvent. When two cuts are
    # merged this holds the latest source's id (see sentence_buffer.merge_cuts).
    utterance_id: str = ""
    # Every STT chunk that composed this sentence (a sentence is usually built
    # from several pushes / merged cuts). Lets each contributing transcription's
    # audio + confidence be joined back for STT-vs-translation error attribution.
    source_utterance_ids: tuple[str, ...] = ()
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
    source_utterance_ids: tuple[str, ...] = (),
) -> SentenceEvent:
    if source is None:
        return SentenceEvent(text=text, incomplete=incomplete, source_utterance_ids=source_utterance_ids)
    return SentenceEvent(
        text=text,
        incomplete=incomplete,
        profile_id=source.profile_id,
        stt_engine=source.engine,
        utterance_id=source.utterance_id,
        source_utterance_ids=source_utterance_ids or ((source.utterance_id,) if source.utterance_id else ()),
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
            "utterance_id": item.utterance_id,
            "source_utterance_ids": list(item.source_utterance_ids),
            "avg_logprob": item.avg_logprob,
            "no_speech_prob": item.no_speech_prob,
        }
    if isinstance(item, dict):
        return {
            "profile_id": item.get("profile_id", ""),
            "stt_engine": item.get("stt_engine", ""),
            "utterance_id": item.get("utterance_id", ""),
            "source_utterance_ids": list(item.get("source_utterance_ids", ())),
            "avg_logprob": item.get("avg_logprob"),
            "no_speech_prob": item.get("no_speech_prob"),
        }
    return {
        "profile_id": "",
        "stt_engine": "",
        "utterance_id": "",
        "source_utterance_ids": [],
        "avg_logprob": None,
        "no_speech_prob": None,
    }
