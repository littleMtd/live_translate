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
from modules.profile_context import profile_state
from modules import profile_control
from modules.translation_engines import effective_engine_chain_names, engine_is_configured

log = get_logger("main")

# Queues connecting pipeline stages
audio_queue    = queue.Queue(maxsize=cfg.audio.queue_maxsize)
text_queue     = queue.Queue(maxsize=cfg.stt.queue_maxsize)
sentence_queue = queue.Queue(maxsize=cfg.translation.queue_maxsize)
subtitle_queue = queue.Queue(maxsize=cfg.subtitle.queue_maxsize)
provisional_queue = queue.Queue(maxsize=cfg.splitter.provisional_queue_maxsize)

stop_event  = threading.Event()
pause_event = threading.Event()   # set = pipeline paused

_LOG_DIR = Path(__file__).parent / "logs"
_CHATGPT_BUNDLE_OUTPUT_ROOT = Path(__file__).parent / "scratch" / "chatgpt_bundles"


def _selected_translation_backend() -> str:
    if cfg.translation.translation_mode == "clip":
        return cfg.clip_engine
    return cfg.live_engine


def _warn_missing_engine_chain_keys() -> list[str]:
    chain = (
        effective_engine_chain_names()
        if _selected_translation_backend() == "anthropic"
        else tuple(cfg.translation.engine_chain)
    )
    available = [name for name in chain
                 if engine_is_configured(name)]
    missing = [name for name in chain if name not in available]
    for name in missing:
        log.warning("Engine %r skipped - API key not set", name)
    return available


def _validate_scene_vision_config() -> None:
    if not cfg.scene.enabled:
        return
    from modules.scene_vision import missing_vision_route_credentials

    missing = missing_vision_route_credentials()
    if missing:
        log.error(
            "Startup error: missing API key for configured scene vision route(s): %s",
            ", ".join(missing),
        )
        sys.exit(1)


def _validate_config(stt_only: bool):
    if stt_only:
        return
    backend = _selected_translation_backend()
    if backend != "anthropic":
        if not engine_is_configured(backend):
            log.error("Startup error: no API key set for translation backend %r", backend)
            sys.exit(1)
        if backend == "nvidia":
            _warn_missing_engine_chain_keys()
    else:
        if (
            cfg.translation.translation_mode == "live"
            and cfg.translation.deepseek_route == "primary"
        ):
            required = [
                name for name in ("deepseek", "openrouter")
                if not engine_is_configured(name)
            ]
            if required:
                log.error(
                    "Startup error: protected DeepSeek route requires API keys for %s",
                    ", ".join(required),
                )
                sys.exit(1)
        available = _warn_missing_engine_chain_keys()
        if not available:
            log.error("Startup error: no API key set for any engine in engine_chain %s",
                      cfg.translation.engine_chain)
            sys.exit(1)
    _validate_scene_vision_config()


def _donation_ocr_command(app_path: Path) -> list[str]:
    profile = cfg.active_streamer_profile if cfg.translation.use_profile else ""
    return [sys.executable, str(app_path), "--profile", profile]


def _handle_signal(sig, frame):
    log.info("Shutdown requested (signal %s)", sig)
    stop_event.set()


def _export_chatgpt_bundle_on_shutdown(*, status: str) -> dict | None:
    """Persist a terminal marker, then export this process's completed run.

    Export is deliberately fail-soft: shutdown outcome and exit status remain
    authoritative even if local artifact creation fails.
    """
    try:
        from utils.chatgpt_bundle import export_bundle
        from utils.runtime_events import runtime_events

        runtime_events.emit(
            "runtime_lifecycle",
            action="shutdown",
            status=status,
        )
        result = export_bundle(
            run_id=runtime_events.run_id,
            log_dir=_LOG_DIR,
            output_root=_CHATGPT_BUNDLE_OUTPUT_ROOT,
            project_root=Path(__file__).parent,
            config_path=_LOG_DIR / "live_translate_config.json",
            audio_root=_LOG_DIR / "audio_dump",
            include_audio=False,
        )
        log.info(
            "ChatGPT runtime bundle exported: %s (%s files, %s bytes)",
            result["output_path"],
            result["file_count"],
            result["total_bytes"],
        )
        return result
    except Exception as exc:
        log.error("Automatic ChatGPT runtime bundle export failed: %s", exc)
        return None


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
    parser.add_argument("--donation-ocr", action="store_true",
                        help="also launch the donation OCR translation panel "
                             "(donation_ocr/app.py) as a side process")
    args = parser.parse_args()

    stt_only = args.stt_only or args.listen
    try:
        _validate_config(stt_only)
        if args.listen:
            _apply_listen_mode_config()

        profile_state.configure_source(
            cfg.active_streamer_profile,
            mode=str(getattr(cfg.translation, "profile_mode", "auto")),
            translation_profile_applied=bool(cfg.translation.use_profile),
            stt_glossary_applied=bool(cfg.stt.use_profile_glossary),
        )
    except (Exception, SystemExit):
        _export_chatgpt_bundle_on_shutdown(status="startup_failed")
        raise

    try:
        from utils.config_export import write as _write_config_json
        _write_config_json()
        log.debug("Config exported for Tauri dashboard")
    except Exception as e:
        log.debug("Config export skipped: %s", e)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("Starting pipeline… (stt-only=%s, listen=%s)", stt_only, args.listen)

    ocr_proc = None
    if args.donation_ocr:
        # Side process, not a pipeline stage. It runs its own Translator in
        # DB-less mode (no SQLite/history-file access), so it never contends
        # with this process's translation DB writes.
        import subprocess
        app_path = Path(__file__).parent / "donation_ocr" / "app.py"
        ocr_proc = subprocess.Popen(_donation_ocr_command(app_path))
        log.info("Donation OCR panel launched (pid=%s)", ocr_proc.pid)

    all_queues = [
        audio_queue,
        text_queue,
        sentence_queue,
        provisional_queue,
        subtitle_queue,
    ]

    try:
        audio_thread = audio_capture.start(audio_queue, stop_event, pause_event)
    except Exception as exc:
        log.error("Audio capture failed to start: %s", exc)
        stop_event.set()
        if ocr_proc is not None and ocr_proc.poll() is None:
            ocr_proc.terminate()
            log.info("Donation OCR panel terminated")
        _export_chatgpt_bundle_on_shutdown(status="startup_failed")
        sys.exit(1)

    threads = [audio_thread, profile_control.start(stop_event)]
    pipeline_error: Exception | None = None
    try:
        threads.append(stt.start(audio_queue, text_queue, stop_event, pause_event))
        threads.append(
            sentence_splitter.start(
                text_queue,
                sentence_queue,
                stop_event,
                pause_event,
                None if stt_only else provisional_queue,
            )
        )

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
            threads.append(
                translator.start(
                    sentence_queue,
                    subtitle_queue,
                    stop_event,
                    pause_event,
                    provisional_queue=provisional_queue,
                )
            )
            if cfg.scene.enabled:
                from modules import scene_context
                threads.append(scene_context.start(stop_event, pause_event))
            log.info("All background threads started. Opening subtitle window (Ctrl+C to quit).")
            subtitle_display.start(subtitle_queue, stop_event, pause_event, all_queues)
    except Exception as exc:
        pipeline_error = exc
        log.error("Pipeline aborted: %s", exc, exc_info=True)
    finally:
        _shutdown_threads(threads, stop_event, cfg.thread_join_timeout)
        if ocr_proc is not None and ocr_proc.poll() is None:
            ocr_proc.terminate()
            log.info("Donation OCR panel terminated")
        _export_chatgpt_bundle_on_shutdown(
            status="failed" if pipeline_error is not None else "completed"
        )
    log.info("Shutdown complete")
    if pipeline_error is not None:
        raise SystemExit(1) from pipeline_error


if __name__ == "__main__":
    main()
