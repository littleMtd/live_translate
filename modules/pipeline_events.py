from dataclasses import dataclass
from typing import Any

from modules.activity_context import ActivitySnapshot
from modules.profile_context import ProfileSnapshot


def _source_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in (str(item).strip() for item in value) if item)


def _aligned_source_values(
    values: Any,
    source_utterance_ids: tuple[str, ...],
    *,
    fallback: float | None = None,
) -> tuple[float | None, ...]:
    count = len(source_utterance_ids)
    if count == 0:
        return ()

    if isinstance(values, (list, tuple)):
        aligned = list(values[:count])
        if len(aligned) < count:
            aligned.extend([None] * (count - len(aligned)))
        return tuple(value if value is None else value for value in aligned)

    if count == 1:
        return (fallback,)
    return tuple(None for _ in source_utterance_ids)


def _numeric_values(values: tuple[float | None, ...]) -> list[float]:
    numeric: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            numeric.append(float(value))
        except (TypeError, ValueError):
            continue
    return numeric


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def source_confidence_summary(
    source_utterance_ids: Any,
    source_avg_logprobs: Any = (),
    source_no_speech_probs: Any = (),
) -> dict[str, Any]:
    source_ids = _source_ids(source_utterance_ids)
    avg_values = _aligned_source_values(source_avg_logprobs, source_ids)
    no_speech_values = _aligned_source_values(source_no_speech_probs, source_ids)
    avg_numeric = _numeric_values(avg_values)
    no_speech_numeric = _numeric_values(no_speech_values)
    return {
        "source_count": len(source_ids),
        "source_avg_logprobs": list(avg_values),
        "min_avg_logprob": min(avg_numeric) if avg_numeric else None,
        "source_no_speech_probs": list(no_speech_values),
        "max_no_speech_prob": max(no_speech_numeric) if no_speech_numeric else None,
    }


@dataclass(frozen=True)
class SegmentInfo:
    start: float | None = None
    end: float | None = None
    text: str = ""
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    # Provider-native word evidence. Scribe calls this field ``logprob``;
    # keeping it separate avoids pretending it is Groq's segment avg_logprob.
    logprob: float | None = None
    word_type: str = ""
    speaker_id: str = ""


@dataclass(frozen=True)
class AudioChunk:
    audio: Any
    overlap_seconds: float = 0.0
    vad_cut_reason: str = ""
    raw_audio_seconds: float = 0.0

    def __len__(self) -> int:
        return len(self.audio)

    def __getitem__(self, key: Any) -> Any:
        return self.audio[key]

    @property
    def dtype(self) -> Any:
        return getattr(self.audio, "dtype", None)


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
    segments: tuple[SegmentInfo, ...] = ()
    overlap_seconds: float = 0.0
    vad_cut_reason: str = ""
    language_probability: float | None = None
    transcription_id: str = ""
    profile_snapshot: ProfileSnapshot | None = None


@dataclass(frozen=True)
class SentenceEvent:
    text: str
    incomplete: bool = False
    profile_id: str = ""
    stt_engine: str = ""
    # Carried through from the source TranscriptionEvent. When two cuts are
    # merged this holds the latest source's id (see sentence_buffer.merge_cuts).
    utterance_id: str = ""
    # Current-source STT chunks for this sentence. Prior carry-forward chunks
    # live in evidence_source_utterance_ids so they do not count as current audio.
    source_utterance_ids: tuple[str, ...] = ()
    evidence_source_utterance_ids: tuple[str, ...] = ()
    source_avg_logprobs: tuple[float | None, ...] = ()
    source_no_speech_probs: tuple[float | None, ...] = ()
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    cut_reason: str = ""
    forced: bool = False
    chunk_count: int = 0
    audio_seconds: float = 0.0
    sentence_id: str = ""
    created_at_utc: str = ""
    enqueued_at_utc: str = ""
    enqueued_at_monotonic: float = 0.0
    activity_snapshot: ActivitySnapshot | None = None
    provisional_id: str = ""
    profile_snapshot: ProfileSnapshot | None = None


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
    evidence_source_utterance_ids: tuple[str, ...] = (),
    source_avg_logprobs: tuple[float | None, ...] = (),
    source_no_speech_probs: tuple[float | None, ...] = (),
    cut_reason: str = "",
    forced: bool = False,
    chunk_count: int = 0,
    audio_seconds: float = 0.0,
    sentence_id: str = "",
    created_at_utc: str = "",
    enqueued_at_utc: str = "",
    enqueued_at_monotonic: float = 0.0,
    activity_snapshot: ActivitySnapshot | None = None,
    provisional_id: str = "",
) -> SentenceEvent:
    source_ids = _source_ids(source_utterance_ids)
    evidence_source_ids = _source_ids(evidence_source_utterance_ids)
    if source is None:
        return SentenceEvent(
            text=text,
            incomplete=incomplete,
            source_utterance_ids=source_ids,
            evidence_source_utterance_ids=evidence_source_ids,
            source_avg_logprobs=_aligned_source_values(source_avg_logprobs, source_ids),
            source_no_speech_probs=_aligned_source_values(source_no_speech_probs, source_ids),
            cut_reason=cut_reason,
            forced=forced,
            chunk_count=chunk_count,
            audio_seconds=audio_seconds,
            sentence_id=sentence_id,
            created_at_utc=created_at_utc,
            enqueued_at_utc=enqueued_at_utc,
            enqueued_at_monotonic=enqueued_at_monotonic,
            activity_snapshot=activity_snapshot,
            provisional_id=provisional_id,
        )
    if not source_ids:
        source_ids = (source.utterance_id,) if source.utterance_id else ()
    return SentenceEvent(
        text=text,
        incomplete=incomplete,
        profile_id=source.profile_id,
        profile_snapshot=source.profile_snapshot,
        stt_engine=source.engine,
        utterance_id=source.utterance_id,
        source_utterance_ids=source_ids,
        evidence_source_utterance_ids=evidence_source_ids,
        source_avg_logprobs=_aligned_source_values(
            source_avg_logprobs, source_ids, fallback=source.avg_logprob
        ),
        source_no_speech_probs=_aligned_source_values(
            source_no_speech_probs, source_ids, fallback=source.no_speech_prob
        ),
        avg_logprob=source.avg_logprob,
        no_speech_prob=source.no_speech_prob,
        cut_reason=cut_reason,
        forced=forced,
        chunk_count=chunk_count,
        audio_seconds=audio_seconds,
        sentence_id=sentence_id,
        created_at_utc=created_at_utc,
        enqueued_at_utc=enqueued_at_utc,
        enqueued_at_monotonic=enqueued_at_monotonic,
        activity_snapshot=activity_snapshot,
        provisional_id=provisional_id,
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
        source_ids = _source_ids(item.source_utterance_ids)
        evidence_source_ids = _source_ids(item.evidence_source_utterance_ids)
        source_avg_logprobs = _aligned_source_values(
            item.source_avg_logprobs, source_ids, fallback=item.avg_logprob
        )
        source_no_speech_probs = _aligned_source_values(
            item.source_no_speech_probs, source_ids, fallback=item.no_speech_prob
        )
        snapshot = item.activity_snapshot
        return {
            "profile_id": item.profile_id,
            "profile_snapshot": item.profile_snapshot,
            "stt_engine": item.stt_engine,
            "utterance_id": item.utterance_id,
            "source_utterance_ids": list(source_ids),
            "evidence_source_utterance_ids": list(evidence_source_ids),
            "evidence_source_count": len(evidence_source_ids),
            "avg_logprob": item.avg_logprob,
            "no_speech_prob": item.no_speech_prob,
            "cut_reason": item.cut_reason,
            "forced": item.forced,
            "chunk_count": item.chunk_count,
            "audio_seconds": item.audio_seconds,
            "sentence_id": item.sentence_id,
            "sentence_created_at_utc": item.created_at_utc,
            "sentence_enqueued_at_utc": item.enqueued_at_utc,
            "sentence_enqueued_at_monotonic": item.enqueued_at_monotonic,
            "activity_snapshot": snapshot,
            "provisional_id": item.provisional_id,
            **source_confidence_summary(
                source_ids,
                source_avg_logprobs,
                source_no_speech_probs,
            ),
        }
    if isinstance(item, dict):
        source_ids = _source_ids(item.get("source_utterance_ids", ()))
        evidence_source_ids = _source_ids(item.get("evidence_source_utterance_ids", ()))
        return {
            "profile_id": item.get("profile_id", ""),
            "profile_snapshot": item.get("profile_snapshot"),
            "stt_engine": item.get("stt_engine", ""),
            "utterance_id": item.get("utterance_id", ""),
            "source_utterance_ids": list(source_ids),
            "evidence_source_utterance_ids": list(evidence_source_ids),
            "evidence_source_count": len(evidence_source_ids),
            "avg_logprob": item.get("avg_logprob"),
            "no_speech_prob": item.get("no_speech_prob"),
            "cut_reason": item.get("cut_reason", ""),
            "forced": bool(item.get("forced", False)),
            "chunk_count": _int_or_zero(item.get("chunk_count")),
            "audio_seconds": item.get("audio_seconds", 0.0),
            "sentence_id": item.get("sentence_id", ""),
            "sentence_created_at_utc": item.get("created_at_utc", ""),
            "sentence_enqueued_at_utc": item.get("enqueued_at_utc", ""),
            "sentence_enqueued_at_monotonic": item.get("enqueued_at_monotonic", 0.0),
            "activity_snapshot": item.get("activity_snapshot"),
            "provisional_id": item.get("provisional_id", ""),
            **source_confidence_summary(
                source_ids,
                item.get("source_avg_logprobs", ()),
                item.get("source_no_speech_probs", ()),
            ),
        }
    return {
        "profile_id": "",
        "profile_snapshot": None,
        "stt_engine": "",
        "utterance_id": "",
        "source_utterance_ids": [],
        "evidence_source_utterance_ids": [],
        "evidence_source_count": 0,
        "avg_logprob": None,
        "no_speech_prob": None,
        "cut_reason": "",
        "forced": False,
        "chunk_count": 0,
        "audio_seconds": 0.0,
        "sentence_id": "",
        "sentence_created_at_utc": "",
        "sentence_enqueued_at_utc": "",
        "sentence_enqueued_at_monotonic": 0.0,
        "activity_snapshot": None,
        "provisional_id": "",
        **source_confidence_summary(()),
    }
