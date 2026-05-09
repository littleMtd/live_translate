"""
Korean live stream real-time subtitle translator.
Usage:
    python main.py              # full pipeline
    python main.py --stt-only   # audio → STT → splitter, print sentences, no API calls
"""
import argparse
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from config import cfg
from utils.logger import get_logger
from modules import audio_capture, stt, sentence_splitter, translator, subtitle_display

log = get_logger("main")

# Queues connecting pipeline stages
audio_queue    = queue.Queue(maxsize=cfg.audio.queue_maxsize)
text_queue     = queue.Queue(maxsize=cfg.stt.queue_maxsize)
sentence_queue = queue.Queue(maxsize=cfg.translation.queue_maxsize)
subtitle_queue = queue.Queue(maxsize=cfg.subtitle.queue_maxsize)

stop_event  = threading.Event()
pause_event = threading.Event()   # set = pipeline paused

_LOG_DIR = Path(__file__).parent / "logs"


_KEY_FOR_ENGINE = {
    "gemini":           lambda: cfg.keys.gemini,
    "claude":           lambda: cfg.keys.anthropic,
    "google_translate": lambda: cfg.keys.google_translate,
    "deepseek":         lambda: cfg.keys.deepseek,
    "deepl":            lambda: cfg.keys.deepl,
}


def _validate_config(stt_only: bool):
    if stt_only:
        return
    available = [name for name in cfg.translation.engine_chain
                 if _KEY_FOR_ENGINE.get(name, lambda: True)()]
    if not available:
        log.error("Startup error: no API key set for any engine in engine_chain %s",
                  cfg.translation.engine_chain)
        sys.exit(1)
    missing = [name for name in cfg.translation.engine_chain if name not in available]
    for name in missing:
        log.warning("Engine %r skipped — API key not set", name)


def _handle_signal(sig, frame):
    log.info("Shutdown requested (signal %s)", sig)
    stop_event.set()


def _stt_printer(sentence_queue: queue.Queue, stop_event: threading.Event) -> threading.Thread:
    """Reads sentences and prints them to console + log file. No API calls."""
    _LOG_DIR.mkdir(exist_ok=True)
    log_path = _LOG_DIR / f"stt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    def run():
        print(f"\n  [STT-only mode] sentences → {log_path}\n", flush=True)
        with open(log_path, "w", encoding="utf-8") as f:
            while not stop_event.is_set():
                try:
                    item = sentence_queue.get(timeout=1)
                except queue.Empty:
                    continue
                ts = datetime.now().strftime("%H:%M:%S")
                flag = " [incomplete]" if item.get("incomplete") else ""
                line = f"[{ts}]{flag} {item['text']}"
                print(line, flush=True)
                f.write(line + "\n")
                f.flush()
        log.info("STT printer stopped")

    t = threading.Thread(target=run, name="STTPrinter", daemon=True)
    t.start()
    return t


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stt-only", action="store_true",
                        help="run STT + splitter only, no translation API calls")
    args = parser.parse_args()

    _validate_config(args.stt_only)

    try:
        from utils.config_export import write as _write_config_json
        _write_config_json()
        log.debug("Config exported for Tauri dashboard")
    except Exception as e:
        log.debug("Config export skipped: %s", e)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("Starting pipeline… (stt-only=%s)", args.stt_only)

    all_queues = [audio_queue, text_queue, sentence_queue, subtitle_queue]

    threads = [
        audio_capture.start(audio_queue, stop_event, pause_event),
        stt.start(audio_queue, text_queue, stop_event, pause_event),
        sentence_splitter.start(text_queue, sentence_queue, stop_event, pause_event),
    ]

    if args.stt_only:
        threads.append(_stt_printer(sentence_queue, stop_event))
        log.info("STT-only mode — press Ctrl+C to stop")
        stop_event.wait()
    else:
        threads.append(translator.start(sentence_queue, subtitle_queue, stop_event, pause_event))
        log.info("All background threads started. Opening subtitle window (Ctrl+C to quit).")
        subtitle_display.start(subtitle_queue, stop_event, pause_event, all_queues)

    stop_event.set()
    for t in threads:
        t.join(timeout=cfg.thread_join_timeout)
        if t.is_alive():
            log.warning("Thread %s did not stop within %ds", t.name, cfg.thread_join_timeout)
    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
