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
from dataclasses import replace
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
    "groq":             lambda: cfg.keys.groq_fallback,
    "nvidia":           lambda: cfg.keys.nvidia,
    "ollama":           lambda: True,
}


def _selected_translation_backend() -> str:
    if cfg.translation.translation_mode == "clip":
        return cfg.clip_engine
    return cfg.live_engine


def _warn_missing_engine_chain_keys() -> list[str]:
    available = [name for name in cfg.translation.engine_chain
                 if _KEY_FOR_ENGINE.get(name, lambda: True)()]
    missing = [name for name in cfg.translation.engine_chain if name not in available]
    for name in missing:
        log.warning("Engine %r skipped - API key not set", name)
    return available


def _validate_config(stt_only: bool):
    if stt_only:
        return
    backend = _selected_translation_backend()
    if backend != "anthropic":
        if not _KEY_FOR_ENGINE.get(backend, lambda: True)():
            log.error("Startup error: no API key set for translation backend %r", backend)
            sys.exit(1)
        if backend == "nvidia":
            _warn_missing_engine_chain_keys()
        return

    available = _warn_missing_engine_chain_keys()
    if not available:
        log.error("Startup error: no API key set for any engine in engine_chain %s",
                  cfg.translation.engine_chain)
        sys.exit(1)


def _handle_signal(sig, frame):
    log.info("Shutdown requested (signal %s)", sig)
    stop_event.set()


def _shutdown_threads(
    threads: list[threading.Thread],
    stop_event: threading.Event,
    join_timeout: float,
    logger=log,
) -> list[threading.Thread]:
    """Signal stop and reverse-join the pipeline.

    Reverse order (consumer-to-producer) means subscribers wind down before
    their upstream producers stop emitting. This only reorders the join
    sequence and warns on stuck threads; it makes no promise that residual
    queue items are consumed before exit. The follow-up two-stage shutdown
    that handles in-flight queue items is tracked as future work (#7b).

    Returns the threads still alive after the join attempt — useful for tests
    and for callers that want to take further action (force terminate, etc.).
    """
    stop_event.set()
    stuck: list[threading.Thread] = []
    for t in reversed(threads):
        t.join(timeout=join_timeout)
        if t.is_alive():
            logger.warning("Thread %s did not stop within %ss", t.name, join_timeout)
            stuck.append(t)
    return stuck


def _apply_listen_mode_config() -> None:
    listen_stt = replace(
        cfg.stt,
        groq_prompt=cfg.stt.listen_groq_prompt,
        no_speech_threshold=cfg.stt.listen_no_speech_threshold,
        avg_logprob_threshold=cfg.stt.listen_avg_logprob_threshold,
    )
    object.__setattr__(cfg, "stt", listen_stt)
    log.info(
        "Listen mode enabled (avg_logprob_threshold=%s, no_speech_threshold=%s)",
        cfg.stt.avg_logprob_threshold,
        cfg.stt.no_speech_threshold,
    )


def _stt_printer(
    sentence_queue: queue.Queue,
    stop_event: threading.Event,
    mode_label: str = "STT-only",
    log_prefix: str = "stt",
) -> threading.Thread:
    """Reads sentences and prints them to console + log file. No API calls."""
    _LOG_DIR.mkdir(exist_ok=True)
    log_path = _LOG_DIR / f"{log_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    def run():
        print(f"\n  [{mode_label} mode] sentences → {log_path}\n", flush=True)
        with open(log_path, "w", encoding="utf-8") as f:
            while not stop_event.is_set():
                try:
                    item = sentence_queue.get(timeout=1)
                except queue.Empty:
                    continue
                ts = datetime.now().strftime("%H:%M:%S")
                flag = " [incomplete]" if item.incomplete else ""
                line = f"[{ts}]{flag} {item.text}"
                print(line, flush=True)
                f.write(line + "\n")
                f.flush()
        log.info("STT printer stopped")

    t = threading.Thread(target=run, name="STTPrinter", daemon=True)
    t.start()
    return t


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stt-only", action="store_true",
                      help="run STT + splitter only, no translation API calls")
    mode.add_argument("--listen", action="store_true",
                      help="listen mode for Korean lyrics/music: STT-only with relaxed filters")
    args = parser.parse_args()

    stt_only = args.stt_only or args.listen
    _validate_config(stt_only)
    if args.listen:
        _apply_listen_mode_config()

    try:
        from utils.config_export import write as _write_config_json
        _write_config_json()
        log.debug("Config exported for Tauri dashboard")
    except Exception as e:
        log.debug("Config export skipped: %s", e)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("Starting pipeline… (stt-only=%s, listen=%s)", stt_only, args.listen)

    all_queues = [audio_queue, text_queue, sentence_queue, subtitle_queue]

    try:
        audio_thread = audio_capture.start(audio_queue, stop_event, pause_event)
    except Exception as exc:
        log.error("Audio capture failed to start: %s", exc)
        sys.exit(1)

    threads = [
        audio_thread,
        stt.start(audio_queue, text_queue, stop_event, pause_event),
        sentence_splitter.start(text_queue, sentence_queue, stop_event, pause_event),
    ]

    if stt_only:
        if args.listen:
            threads.append(_stt_printer(sentence_queue, stop_event, "listen", "listen"))
            log.info("Listen mode — press Ctrl+C to stop")
        else:
            threads.append(_stt_printer(sentence_queue, stop_event))
            log.info("STT-only mode — press Ctrl+C to stop")
        while not stop_event.is_set():
            stop_event.wait(1.0)
    else:
        threads.append(translator.start(sentence_queue, subtitle_queue, stop_event, pause_event))
        log.info("All background threads started. Opening subtitle window (Ctrl+C to quit).")
        subtitle_display.start(subtitle_queue, stop_event, pause_event, all_queues)

    _shutdown_threads(threads, stop_event, cfg.thread_join_timeout)
    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
