from __future__ import annotations

from dataclasses import dataclass

from modules.pipeline_events import TranscriptionEvent, transcription_text
from utils.text_heuristics import (
    SENTENCE_COMPLETE_ENDINGS,
    SENTENCE_INCOMPLETE_ENDINGS,
    STT_INSIGNIFICANT_RE,
)


_COMPLETE_ENDINGS = SENTENCE_COMPLETE_ENDINGS
_INCOMPLETE_ENDINGS = SENTENCE_INCOMPLETE_ENDINGS

# Task #10 B1 v1 — only sentence-final PUNCTUATION is trusted as an internal
# split boundary. Bare morpheme endings (다/요/죠…) are intentionally NOT used
# here: live STT tokenization is unstable and `다`(=all) etc. routinely appear
# mid-utterance, so morpheme splitting is deferred to a later task (§11.11 #2).
_BOUNDARY_CHARS = ".?!~…。？！"

# Codex §11.11 #1/#5 thresholds.
_MIN_PREFIX_SIGNIFICANT = 6   # prefix shorter than this → don't split
_MAX_TRIVIAL_RESIDUAL = 3     # residual at or below this → drop, don't carry


def _significant_len(value: str) -> int:
    return len(STT_INSIGNIFICANT_RE.sub("", value))


def split_complete_prefix(text: str) -> tuple[str, str]:
    """Split `text` at its LAST sentence-final punctuation boundary.

    Returns `(complete_prefix, residual)`:
      * `complete_prefix` ends at (and includes) the last boundary punctuation;
      * `residual` is whatever trailing fragment follows it (no boundary char,
        by construction the last boundary is the maximum index).

    If no boundary punctuation is present, returns `("", text)` — there is no
    safe internal cut, caller keeps the original whole-blob behavior.
    """
    text = text.strip()
    if not text:
        return "", ""

    last = -1
    for index, char in enumerate(text):
        if char in _BOUNDARY_CHARS:
            last = index
    if last < 0:
        return "", text

    prefix = text[: last + 1].strip()
    residual = text[last + 1 :].strip()
    return prefix, residual


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
            buffered = self._buffer.strip()
            prefix, residual = split_complete_prefix(buffered)

            # Recoverable: a punctuation-bounded complete prefix that is long
            # enough to be worth emitting on its own (§11.11 #5, N=6).
            if prefix and _significant_len(prefix) >= _MIN_PREFIX_SIGNIFICANT:
                cut = SentenceCut(
                    text=prefix,
                    incomplete=False,
                    source=self._latest_source,
                    elapsed=elapsed,
                    forced=True,
                )
                if residual and _significant_len(residual) > _MAX_TRIVIAL_RESIDUAL:
                    # Residual policy (c): carry it back into the buffer and
                    # restart the time-box clock so the leftover fragment does
                    # not immediately force-cut on the next pop (§11.11 #1).
                    self._buffer = residual
                    self._first_token_time = now
                else:
                    # Trivial / empty residual → drop, full reset.
                    self.reset()
                return cut

            # Not recoverable (no punctuation boundary, or prefix too short):
            # keep the original whole-blob forced behavior unchanged.
            cut = SentenceCut(
                text=buffered,
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
