import queue
import threading

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import start_daemon_thread, wait_while_paused
from utils.queue_utils import put_latest
from modules.pipeline_events import transcription_to_sentence
from modules.sentence_buffer import SentenceBuffer, SentenceCut, is_complete

log = get_logger("sentence_splitter")


def _is_complete(text: str) -> bool:
    return is_complete(text)


def start(text_queue: queue.Queue, sentence_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        import time
        buffer = SentenceBuffer()
        pending_incomplete: SentenceCut | None = None

        def emit_cut(cut: SentenceCut) -> None:
            if cut.forced:
                log.debug("Force cut after %.1fs (incomplete=%s)", cut.elapsed, cut.incomplete)
            log.info("Sentence ready (incomplete=%s): %s", cut.incomplete, cut.text)
            event = transcription_to_sentence(cut.text, cut.incomplete, cut.source)
            metrics.increment("sentence.emitted")
            put_latest(sentence_queue, event, log, "sentence_queue")
            metrics.log_summary_if_due()

        def merge_cuts(first: SentenceCut, second: SentenceCut) -> SentenceCut:
            return SentenceCut(
                text=f"{first.text} {second.text}".strip(),
                incomplete=second.incomplete,
                source=second.source or first.source,
                elapsed=first.elapsed + second.elapsed,
                forced=first.forced or second.forced,
            )

        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                buffer.reset()
                pending_incomplete = None
                wait_while_paused(stop_event, pause_event)
                # Drain tokens that accumulated while paused so they don't
                # appear as fresh content after resume.
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
                    emit_cut(merge_cuts(pending_incomplete, cut))
                    pending_incomplete = None
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
