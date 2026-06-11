import queue
import threading

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import start_daemon_thread, wait_while_paused
from utils.queue_utils import put_drop_oldest
from utils.runtime_events import runtime_events
from modules.pipeline_events import source_confidence_summary, transcription_to_sentence
from modules.sentence_buffer import SentenceBuffer, SentenceCut, is_complete

log = get_logger("sentence_splitter")

_DEFAULT_MAX_MERGE_SOURCE_COUNT = 2
_DEFAULT_MAX_MERGE_TEXT_CHARS = 120


def _is_complete(text: str) -> bool:
    return is_complete(text)


def _int_setting(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_setting(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _bool_setting(value: object, default: bool) -> bool:
    return bool(value) if isinstance(value, bool) else default


def _merge_source_count(first: SentenceCut, second: SentenceCut) -> int:
    if first.source_utterance_ids or second.source_utterance_ids:
        return len(first.source_utterance_ids) + len(second.source_utterance_ids)
    return first.chunk_count + second.chunk_count


def _merged_text_len(first: SentenceCut, second: SentenceCut) -> int:
    return len(f"{first.text} {second.text}".strip())


def _can_merge_cuts(first: SentenceCut, second: SentenceCut) -> bool:
    max_sources = _int_setting(
        getattr(cfg.splitter, "max_merge_source_count", _DEFAULT_MAX_MERGE_SOURCE_COUNT),
        _DEFAULT_MAX_MERGE_SOURCE_COUNT,
    )
    max_chars = _int_setting(
        getattr(cfg.splitter, "max_merge_text_chars", _DEFAULT_MAX_MERGE_TEXT_CHARS),
        _DEFAULT_MAX_MERGE_TEXT_CHARS,
    )

    if max_sources > 0 and _merge_source_count(first, second) > max_sources:
        return False
    if max_chars > 0 and _merged_text_len(first, second) > max_chars:
        return False
    return True


def _merge_cuts(first: SentenceCut, second: SentenceCut) -> SentenceCut:
    return SentenceCut(
        text=f"{first.text} {second.text}".strip(),
        incomplete=second.incomplete,
        source=second.source or first.source,
        elapsed=first.elapsed + second.elapsed,
        forced=first.forced or second.forced,
        # A merge means an incomplete cut was stitched to its follow-up;
        # surface that distinctly while summing the constituents' tallies.
        cut_reason=f"merged:{first.cut_reason}+{second.cut_reason}",
        chunk_count=first.chunk_count + second.chunk_count,
        audio_seconds=round(first.audio_seconds + second.audio_seconds, 3),
        source_utterance_ids=first.source_utterance_ids + second.source_utterance_ids,
        evidence_source_utterance_ids=(
            first.evidence_source_utterance_ids + second.evidence_source_utterance_ids
        ),
        source_avg_logprobs=first.source_avg_logprobs + second.source_avg_logprobs,
        source_no_speech_probs=first.source_no_speech_probs + second.source_no_speech_probs,
    )


def start(text_queue: queue.Queue, sentence_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        import time
        buffer = SentenceBuffer(
            segment_gap_split_enabled=_bool_setting(
                getattr(cfg.splitter, "segment_gap_split_enabled", False),
                False,
            ),
            segment_gap_seconds=_float_setting(
                getattr(cfg.splitter, "segment_gap_seconds", 0.6),
                0.6,
            ),
            silence_complete_enabled=_bool_setting(
                getattr(cfg.splitter, "silence_complete_enabled", False),
                False,
            ),
        )
        pending_incomplete: SentenceCut | None = None

        def emit_cut(cut: SentenceCut) -> None:
            if cut.forced:
                log.debug("Force cut after %.1fs (incomplete=%s)", cut.elapsed, cut.incomplete)
            log.info("Sentence ready (incomplete=%s): %s", cut.incomplete, cut.text)
            source = cut.source
            runtime_events.emit(
                "sentence",
                utterance_id=source.utterance_id if source else "",
                profile_id=source.profile_id if source else "",
                stt_engine=source.engine if source else "",
                cut_reason=cut.cut_reason,
                incomplete=cut.incomplete,
                forced=cut.forced,
                chunk_count=cut.chunk_count,
                audio_seconds=cut.audio_seconds,
                source_utterance_ids=list(cut.source_utterance_ids),
                evidence_source_utterance_ids=list(cut.evidence_source_utterance_ids),
                evidence_source_count=len(cut.evidence_source_utterance_ids),
                **source_confidence_summary(
                    cut.source_utterance_ids,
                    cut.source_avg_logprobs,
                    cut.source_no_speech_probs,
                ),
                text_len=len(cut.text or ""),
                elapsed_ms=round(cut.elapsed * 1000, 2),
            )
            event = transcription_to_sentence(
                cut.text,
                cut.incomplete,
                cut.source,
                cut.source_utterance_ids,
                cut.evidence_source_utterance_ids,
                cut.source_avg_logprobs,
                cut.source_no_speech_probs,
                cut.cut_reason,
                cut.forced,
                cut.chunk_count,
                cut.audio_seconds,
            )
            metrics.increment("sentence.emitted")
            put_drop_oldest(sentence_queue, event, log, "sentence_queue")
            metrics.log_summary_if_due()

        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                buffer.reset()
                pending_incomplete = None
                wait_while_paused(stop_event, pause_event)
                # Drain tokens that accumulated while paused so they don't
                # appear as fresh content after resume.
                # L14 (known, accepted): the STT producer may still be finishing
                # one item during this drain, so at most one stale token can
                # slip through after resume — harmless for live subtitles.
                while True:
                    try:
                        text_queue.get_nowait()
                    except queue.Empty:
                        break
                continue

            # Wait up to 100 ms for the first new token, then drain the rest immediately.
            # This single blocking call replaces the two time.sleep(0.1) paths below.
            try:
                token = text_queue.get(timeout=0.1)
                buffer.push(token, time.monotonic())
                while True:
                    try:
                        token = text_queue.get_nowait()
                        buffer.push(token, time.monotonic())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass  # no new tokens within 100 ms — fall through to check cut

            cut = buffer.pop_ready(
                time.monotonic(),
                min_wait_seconds=cfg.splitter.min_wait_seconds,
                force_cut_seconds=cfg.splitter.force_cut_seconds,
            )
            if cut:
                if pending_incomplete is not None:
                    if _can_merge_cuts(pending_incomplete, cut):
                        emit_cut(_merge_cuts(pending_incomplete, cut))
                        pending_incomplete = None
                    else:
                        log.info(
                            "Sentence merge skipped: sources=%d chars=%d reason=%s+%s",
                            _merge_source_count(pending_incomplete, cut),
                            _merged_text_len(pending_incomplete, cut),
                            pending_incomplete.cut_reason,
                            cut.cut_reason,
                        )
                        metrics.increment("sentence.merge_skipped")
                        emit_cut(pending_incomplete)
                        if cut.incomplete:
                            pending_incomplete = cut
                            metrics.increment("sentence.incomplete_buffered")
                            log.info("Sentence buffered for next chunk: %s", cut.text)
                        else:
                            pending_incomplete = None
                            emit_cut(cut)
                elif cut.incomplete:
                    pending_incomplete = cut
                    metrics.increment("sentence.incomplete_buffered")
                    log.info("Sentence buffered for next chunk: %s", cut.text)
                else:
                    emit_cut(cut)

        if pending_incomplete is not None:
            emit_cut(pending_incomplete)
        log.info("Sentence splitter stopped")

    return start_daemon_thread("SentenceSplitter", run)


if __name__ == "__main__":
    import time as _time
    tq: queue.Queue = queue.Queue()
    sq: queue.Queue = queue.Queue()
    stop = threading.Event()
    start(tq, sq, stop)

    samples = [
        "안녕하세요",
        "오늘은 날씨가 좋은데",
        "진짜 대박이에요",
        "ㅋㅋㅋ",
    ]
    for s in samples:
        tq.put(s)
        _time.sleep(1)

    _time.sleep(cfg.splitter.force_cut_seconds + 1)
    stop.set()

    while not sq.empty():
        print(sq.get())
