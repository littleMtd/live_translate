from __future__ import annotations

from dataclasses import dataclass

from modules.pipeline_events import TranscriptionEvent, transcription_text
from utils.text_heuristics import SENTENCE_COMPLETE_ENDINGS, SENTENCE_INCOMPLETE_ENDINGS


_COMPLETE_ENDINGS = SENTENCE_COMPLETE_ENDINGS
_INCOMPLETE_ENDINGS = SENTENCE_INCOMPLETE_ENDINGS


@dataclass(frozen=True)
class SentenceCut:
    text: str
    incomplete: bool
    source: TranscriptionEvent | None
    elapsed: float
    forced: bool


def is_complete(text: str) -> bool:
    stripped = text.rstrip()
    for ending in _INCOMPLETE_ENDINGS:
        if stripped.endswith(ending):
            return False
    for ending in _COMPLETE_ENDINGS:
        if stripped.endswith(ending):
            return True
    return False


class SentenceBuffer:
    def __init__(self):
        self._buffer = ""
        self._first_token_time: float | None = None
        self._latest_source: TranscriptionEvent | None = None

    def reset(self) -> None:
        self._buffer = ""
        self._first_token_time = None
        self._latest_source = None

    def push(self, token: str | TranscriptionEvent, now: float) -> None:
        token_text = transcription_text(token)
        if self._first_token_time is None:
            self._first_token_time = now
        if isinstance(token, TranscriptionEvent):
            self._latest_source = token
        self._buffer = (self._buffer + " " + token_text).strip() if self._buffer else token_text

    def pop_ready(
        self,
        now: float,
        *,
        min_wait_seconds: float,
        force_cut_seconds: float,
    ) -> SentenceCut | None:
        if not self._buffer or self._first_token_time is None:
            return None

        elapsed = now - self._first_token_time
        forced = elapsed >= force_cut_seconds
        complete = is_complete(self._buffer)

        if forced:
            cut = SentenceCut(
                text=self._buffer.strip(),
                incomplete=not complete,
                source=self._latest_source,
                elapsed=elapsed,
                forced=True,
            )
            self.reset()
            return cut

        if elapsed >= min_wait_seconds and complete:
            cut = SentenceCut(
                text=self._buffer.strip(),
                incomplete=False,
                source=self._latest_source,
                elapsed=elapsed,
                forced=False,
            )
            self.reset()
            return cut

        return None
