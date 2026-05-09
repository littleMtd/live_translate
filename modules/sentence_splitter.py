import queue
import threading

from config import cfg
from utils.logger import get_logger
from utils.queue_utils import drain_put

log = get_logger("sentence_splitter")

# Korean endings that signal a complete thought — safe to cut here.
# Sorted longest-first so that e.g. "겠어" is checked before "어",
# preventing a short ending from shadowing a longer one.
_COMPLETE_ENDINGS = tuple(sorted([
    "겠어", "겠다", "구나",
    "ㅋㅋ", "ㅎㅎ", "ㅠㅠ",
    "죠", "요", "다", "어", "아", "네", "예", "야", "지", "군", "ㅠ",
    "!", "?", "~",
], key=len, reverse=True))

# Endings that mean the sentence is still ongoing — keep accumulating.
# Also sorted longest-first for consistency.
_INCOMPLETE_ENDINGS = tuple(sorted([
    "는데", "니까", "거든", "잖아", "하고", "이고", "이서", "아서", "어서", "으로", "에서",
    "고", "서", "면",
], key=len, reverse=True))


def _is_complete(text: str) -> bool:
    stripped = text.rstrip()
    for ending in _INCOMPLETE_ENDINGS:
        if stripped.endswith(ending):
            return False
    for ending in _COMPLETE_ENDINGS:
        if stripped.endswith(ending):
            return True
    return False


def start(text_queue: queue.Queue, sentence_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        import time
        buffer = ""
        first_token_time: float | None = None

        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                buffer = ""
                first_token_time = None
                while pause_event.is_set() and not stop_event.is_set():
                    stop_event.wait(timeout=0.5)
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
                if first_token_time is None:
                    first_token_time = time.monotonic()
                buffer = (buffer + " " + token).strip() if buffer else token
                while True:
                    try:
                        token = text_queue.get_nowait()
                        buffer = (buffer + " " + token).strip()
                    except queue.Empty:
                        break
            except queue.Empty:
                pass  # no new tokens within 100 ms — fall through to check cut

            if not buffer or first_token_time is None:
                continue

            elapsed = time.monotonic() - first_token_time

            should_cut = False
            incomplete = False

            if elapsed >= cfg.splitter.force_cut_seconds:
                should_cut = True
                incomplete = not _is_complete(buffer)
                log.debug("Force cut after %.1fs (incomplete=%s)", elapsed, incomplete)
            elif elapsed >= cfg.splitter.min_wait_seconds and _is_complete(buffer):
                should_cut = True
            # else: keep accumulating until complete ending or force cut

            if should_cut:
                sentence = buffer.strip()
                log.info("Sentence ready (incomplete=%s): %s", incomplete, sentence)
                drained = drain_put(sentence_queue, {"text": sentence, "incomplete": incomplete})
                if drained:
                    log.warning("sentence_queue backlog cleared (%d), keeping latest", drained)
                buffer = ""
                first_token_time = None

        log.info("Sentence splitter stopped")

    t = threading.Thread(target=run, name="SentenceSplitter", daemon=True)
    t.start()
    return t


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
