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

    def __getitem__(self, key: str):
        if key == "text":
            return self.text
        if key == "incomplete":
            return self.incomplete
        if key == "profile_id":
            return self.profile_id
        if key == "stt_engine":
            return self.stt_engine
        if key == "avg_logprob":
            return self.avg_logprob
        if key == "no_speech_prob":
            return self.no_speech_prob
        raise KeyError(key)

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def transcription_text(item: str | TranscriptionEvent) -> str:
    if isinstance(item, TranscriptionEvent):
        return item.text
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


def sentence_text(item: dict | SentenceEvent) -> str:
    if isinstance(item, SentenceEvent):
        return item.text
    return item["text"]


def sentence_incomplete(item: dict | SentenceEvent) -> bool:
    if isinstance(item, SentenceEvent):
        return item.incomplete
    return item.get("incomplete", False)
