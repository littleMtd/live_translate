import os; os.environ["LIVE_TRANSLATE_RUN_KIND"] = "cafe_clip"
"""Subprocess entry point: stdout is JSONL; diagnostics go to stderr."""
import argparse
import json
import logging
import math
from pathlib import Path
import sys
import uuid

if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def configure_logging():
    # Configure the repo handler before importing any module that logs.
    from utils import logger
    logger._configure_root()
    loggers = [logging.getLogger()] + [
        value for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)]
    for log in loggers:
        for handler in log.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setStream(sys.stderr)

class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)

def main(argv=None):
    job = uuid.uuid4().hex
    def progress(stage, **fields):
        print(json.dumps({"job": job, "stage": stage, **fields}, ensure_ascii=False), flush=True)
    try:
        configure_logging()
        parser = JsonParser(description="Offline subtitles: stdout is JSONL; diagnostics go to stderr.")
        parser.add_argument("mode", choices=("translate", "transcribe"))
        parser.add_argument("--input", type=Path)
        parser.add_argument("--profile", default="")
        parser.add_argument("--out-dir", type=Path)
        parser.add_argument("--estimate", action="store_true")
        parser.add_argument("--ffmpeg")
        parser.add_argument("--duration", type=float,
                            help="transcribe --estimate only: media length in seconds, no input file needed")
        args = parser.parse_args(argv)
        from modules import offline_subtitles as offline
        from config import cfg
        Error = offline.OfflineSubtitleError
        duration_only = args.mode == "transcribe" and args.estimate and args.duration is not None
        if args.duration is not None and not duration_only:
            raise Error("--duration is only valid with transcribe --estimate")
        if duration_only and not (math.isfinite(args.duration) and args.duration > 0):
            raise Error("--duration must be a positive number of seconds")
        if duration_only and args.duration > 72 * 3600:  # bounds slices(); SOOP VODs are hours, not days
            raise Error("--duration must not exceed 72 hours")
        if not duration_only and (args.input is None or not args.input.is_file()):
            raise Error("input file not found")
        if not args.estimate and args.out_dir is None:
            raise Error("--out-dir is required for output")
        if args.mode == "translate":
            if args.input.suffix.lower() not in (".vtt", ".srt"):
                raise ValueError("translate input must be .vtt or .srt")
            cues = offline.parse_subtitles(args.input.read_text(encoding="utf-8-sig"))
            if not cues:
                raise ValueError("no usable subtitle cues")
            duration = 0
        elif duration_only:
            # Estimating needs only the length; avoids building a media-length file just to probe it.
            duration, cues = args.duration, []
        else:
            encoder, probe = offline.media_tools(args.ffmpeg)
            duration = offline.media_duration(args.input, probe)
            cues = []
        if args.estimate:
            from modules.translation_prompts import get_translation_profile_facts
            progress("estimate", **offline.estimate(
                cues, duration, cfg.stt.groq_model,
                facts=get_translation_profile_facts(args.profile)))
            return 0
        snapshot = offline.profile_scope(args.profile)
        if args.mode == "transcribe":
            if not cfg.keys.groq:
                raise offline.OfflineSubtitleError("Groq API key is missing")
            from groq import Groq
            from modules.profile_context import bind_profile_snapshot
            from modules.activity_context import bind_activity_snapshot, capture_activity_snapshot
            with bind_profile_snapshot(snapshot), bind_activity_snapshot(capture_activity_snapshot("")):
                prompt = offline.stt_prompt(snapshot)
            client = Groq(api_key=cfg.keys.groq, max_retries=0, timeout=offline.OFFLINE_STT_TIMEOUT_SECONDS)
            try:
                cues = offline.transcribe(args.input, duration, encoder, client, prompt,
                                          cfg.stt.groq_model, progress)
            finally:
                client.close()
            if not cues:
                raise ValueError("no usable transcription segments")
        from modules.translation_engines import DeepSeekTranslationEngine
        engine = DeepSeekTranslationEngine()
        if not engine.available:
            raise offline.OfflineSubtitleError("DeepSeek API key is missing")
        targets, report = offline.translate(cues, snapshot, engine, progress)
        report["job"] = job
        files = offline.deliver(args.out_dir, args.input.stem, cues, targets, report)
        progress("done", files=files, report=report)
        return 0
    except KeyboardInterrupt:
        progress("error", message="cancelled")
        return 130
    except Exception as exc:
        # Provider exceptions may contain credentials/request data. Never echo them;
        # only our own validation messages are safe to show.
        from modules.offline_subtitles import OfflineSubtitleError
        message = str(exc) if isinstance(exc, OfflineSubtitleError) else "offline job failed"
        progress("error", message=message, error_type=type(exc).__name__)
        if isinstance(exc, (ValueError, FileExistsError)):
            print(str(exc), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
