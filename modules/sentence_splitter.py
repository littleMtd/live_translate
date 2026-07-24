import queue
import threading

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import start_daemon_thread, wait_while_paused
from utils.queue_utils import put_drop_oldest
from utils.runtime_events import runtime_events
from modules.pipeline_events import (
    TranscriptionEvent,
    source_confidence_summary,
    transcription_text,
    transcription_to_sentence,
)
from modules.sentence_buffer import SentenceBuffer, SentenceCut, is_complete
from modules.sentence_hold_shadow import (
    UnfinishedTail,
    analyze_unfinished_tail,
    evaluate_next_chunk,
)

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
        pending_incomplete_since: float | None = None
        pending_incomplete_timeout = _float_setting(
            getattr(cfg.splitter, "pending_incomplete_timeout_seconds", 8.0),
            8.0,
        )
        shadow_sequence = 0
        active_shadow: dict[str, object] | None = None

        def finish_shadow_without_chunk(reason: str, now: float) -> None:
            nonlocal active_shadow
            if active_shadow is None:
                return
            started = float(active_shadow["started"])
            runtime_events.emit(
                "sentence_hold_shadow",
                phase="outcome",
                shadow_id=active_shadow["shadow_id"],
                signals=active_shadow["signals"],
                observed_next_chunk=False,
                outcome_reason=reason,
                observed_wait_ms=round(max(0.0, now - started) * 1000, 2),
                within_300ms=False,
                within_500ms=False,
                raw_continuation_heuristic=False,
                structural_resolution=False,
                useful_merge_heuristic=False,
            )
            active_shadow = None

        def start_shadow(cut: SentenceCut, now: float, disposition: str) -> None:
            nonlocal shadow_sequence, active_shadow
            analysis = analyze_unfinished_tail(cut.text, forced=cut.forced)
            if not analysis.signals:
                return
            if active_shadow is not None:
                finish_shadow_without_chunk("superseded", now)
            shadow_sequence += 1
            shadow_id = f"sentence-hold-{shadow_sequence}"
            active_shadow = {
                "shadow_id": shadow_id,
                "started": now,
                "text": cut.text,
                "analysis": analysis,
                "signals": list(analysis.signals),
            }
            source = cut.source
            runtime_events.emit(
                "sentence_hold_shadow",
                phase="candidate",
                shadow_id=shadow_id,
                disposition=disposition,
                utterance_id=source.utterance_id if source else "",
                profile_id=source.profile_id if source else "",
                cut_reason=cut.cut_reason,
                incomplete=cut.incomplete,
                forced=cut.forced,
                signals=list(analysis.signals),
                matched_ending=analysis.matched_ending,
                unclosed_delimiters=list(analysis.unclosed_delimiters),
                candidate_text=cut.text,
                candidate_text_len=len(cut.text),
                source_utterance_ids=list(cut.source_utterance_ids),
            )

        def observe_next_chunk(
            token: str | TranscriptionEvent,
            now: float,
        ) -> dict[str, object] | None:
            nonlocal active_shadow
            if active_shadow is None:
                return None
            started = float(active_shadow["started"])
            delay_ms = round(max(0.0, now - started) * 1000, 2)
            analysis = active_shadow["analysis"]
            assert isinstance(analysis, UnfinishedTail)
            evaluation = evaluate_next_chunk(
                str(active_shadow["text"]),
                transcription_text(token),
                analysis,
            )
            payload: dict[str, object] = {
                "phase": "outcome",
                "shadow_id": active_shadow["shadow_id"],
                "signals": active_shadow["signals"],
                "observed_next_chunk": True,
                "outcome_reason": "next_stt_chunk",
                "next_chunk_delay_ms": delay_ms,
                "within_300ms": delay_ms <= 300,
                "within_500ms": delay_ms <= 500,
                "next_chunk_utterance_id": (
                    token.utterance_id if isinstance(token, TranscriptionEvent) else ""
                ),
                **evaluation,
            }
            active_shadow = None
            return payload

        def emit_cut(
            cut: SentenceCut,
            *,
            track_shadow: bool = True,
            shadow_started_at: float | None = None,
        ) -> None:
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
            if track_shadow:
                start_shadow(
                    cut,
                    time.monotonic() if shadow_started_at is None else shadow_started_at,
                    "emitted",
                )
            metrics.log_summary_if_due()

        def buffer_incomplete(cut: SentenceCut, now: float) -> None:
            nonlocal pending_incomplete, pending_incomplete_since
            pending_incomplete = cut
            pending_incomplete_since = now
            metrics.increment("sentence.incomplete_buffered")
            log.info("Sentence buffered for next chunk: %s", cut.text)
            start_shadow(cut, now, "buffered")

        def clear_pending() -> None:
            nonlocal pending_incomplete, pending_incomplete_since
            pending_incomplete = None
            pending_incomplete_since = None

        def flush_pending_if_timed_out(now: float) -> None:
            nonlocal pending_incomplete, pending_incomplete_since
            if pending_incomplete is None or pending_incomplete_timeout <= 0:
                return
            if pending_incomplete_since is None:
                pending_incomplete_since = now
                return
            if now - pending_incomplete_since < pending_incomplete_timeout:
                return
            metrics.increment("sentence.incomplete_timeout")
            log.info("Sentence pending incomplete timed out: %s", pending_incomplete.text)
            finish_shadow_without_chunk("pending_incomplete_timeout", now)
            emit_cut(pending_incomplete, track_shadow=False)
            clear_pending()

        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                buffer.reset()
                finish_shadow_without_chunk("pipeline_paused", time.monotonic())
                clear_pending()
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
            deferred_shadow_outcomes: list[dict[str, object]] = []
            try:
                token = text_queue.get(timeout=0.1)
                received_at = time.monotonic()
                if shadow_outcome := observe_next_chunk(token, received_at):
                    deferred_shadow_outcomes.append(shadow_outcome)
                buffer.push(token, received_at)
                while True:
                    try:
                        token = text_queue.get_nowait()
                        received_at = time.monotonic()
                        if shadow_outcome := observe_next_chunk(token, received_at):
                            deferred_shadow_outcomes.append(shadow_outcome)
                        buffer.push(token, received_at)
                    except queue.Empty:
                        break
            except queue.Empty:
                pass  # no new tokens within 100 ms — fall through to check cut

            now = time.monotonic()
            flush_pending_if_timed_out(now)
            cut = buffer.pop_ready(
                now,
                min_wait_seconds=cfg.splitter.min_wait_seconds,
                force_cut_seconds=cfg.splitter.force_cut_seconds,
            )
            if cut:
                if pending_incomplete is not None:
                    if _can_merge_cuts(pending_incomplete, cut):
                        emit_cut(
                            _merge_cuts(pending_incomplete, cut),
                            shadow_started_at=now,
                        )
                        clear_pending()
                    else:
                        log.info(
                            "Sentence merge skipped: sources=%d chars=%d reason=%s+%s",
                            _merge_source_count(pending_incomplete, cut),
                            _merged_text_len(pending_incomplete, cut),
                            pending_incomplete.cut_reason,
                            cut.cut_reason,
                        )
                        metrics.increment("sentence.merge_skipped")
                        emit_cut(
                            pending_incomplete,
                            track_shadow=False,
                            shadow_started_at=now,
                        )
                        if cut.incomplete:
                            buffer_incomplete(cut, now)
                        else:
                            clear_pending()
                            emit_cut(cut, shadow_started_at=now)
                elif cut.incomplete:
                    buffer_incomplete(cut, now)
                else:
                    emit_cut(cut, shadow_started_at=now)

            # Persist after buffer/cut behavior has completed so shadow I/O
            # cannot delay the observed token's admission or its cut decision.
            for shadow_outcome in deferred_shadow_outcomes:
                runtime_events.emit("sentence_hold_shadow", **shadow_outcome)

        # Stop: flush the not-yet-cut tail of the buffer so the last sentence
        # isn't dropped, merging it with a pending incomplete cut when allowed
        # (same policy as the main loop).
        final_cut = buffer.flush(time.monotonic())
        if pending_incomplete is not None and final_cut is not None:
            if _can_merge_cuts(pending_incomplete, final_cut):
                emit_cut(_merge_cuts(pending_incomplete, final_cut))
            else:
                emit_cut(pending_incomplete, track_shadow=False)
                emit_cut(final_cut)
        elif pending_incomplete is not None:
            emit_cut(pending_incomplete, track_shadow=False)
        elif final_cut is not None:
            emit_cut(final_cut)
        finish_shadow_without_chunk("splitter_stopped", time.monotonic())
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
