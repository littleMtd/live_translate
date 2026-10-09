import os; os.environ["LIVE_TRANSLATE_RUN_KIND"] = "cafe_clip"
"""Subprocess entry point: stdout is JSONL; diagnostics go to stderr."""
import argparse
import json
import logging
import math
import re
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

def build_translator(name, cfg, offline, progress, gemini_model=None):
    """Checked before any paid call, so a missing key never wastes a transcription."""
    if name == "gemini":
        if not cfg.keys.gemini:
            raise offline.OfflineSubtitleError("Gemini API key is missing")
        return offline.GeminiTranslator(cfg.keys.gemini, gemini_model or offline.GEMINI_MODEL, progress=progress)
    from modules.translation_engines import DeepSeekTranslationEngine
    engine = DeepSeekTranslationEngine()
    if not engine.available:
        raise offline.OfflineSubtitleError("DeepSeek API key is missing")
    return engine

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
        parser.add_argument("--translator", choices=("gemini", "deepseek"), default="gemini",
                            help="translation provider; no automatic fallback between them")
        parser.add_argument("--gemini-model", help="override the Gemini model (default gemini-3.8-flash)")
        parser.add_argument("--stt", choices=("elevenlabs", "groq"), default="elevenlabs",
                            help="transcription provider; no automatic fallback between them")
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
        if args.gemini_model is not None and (args.translator != "gemini"
                                              or not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,63}", args.gemini_model)):
            raise Error("--gemini-model must be a Gemini model name and needs --translator gemini")
        if not args.estimate and args.out_dir is None:
            raise Error("--out-dir is required for output")
        if args.mode == "translate":
            if args.input.suffix.lower() not in (".vtt", ".srt"):
                raise Error("translate input must be .vtt or .srt")
            cues = offline.parse_subtitles(args.input.read_text(encoding="utf-8-sig"))
            if not cues:
                raise Error("no usable subtitle cues")
            duration = 0
        elif duration_only:
            # Estimating needs only the length; avoids building a media-length file just to probe it.
            duration, cues = args.duration, []
        else:
            encoder, probe = offline.media_tools(args.ffmpeg)
            duration = offline.media_duration(args.input, probe)
            cues = []
        stt_model = cfg.stt.elevenlabs_model if args.stt == "elevenlabs" else cfg.stt.groq_model
        if args.estimate:
            from modules.translation_prompts import get_translation_profile_facts
            scope = offline.profile_scope(args.profile)
            # Same profile normalization as the real job, so the Gemini request cap is not underestimated.
            sources = offline.normalized_sources(cues, scope) if cues else None
            keyterms = len(offline.offline_keyterms(scope)) if args.stt == "elevenlabs" and duration else 0
            progress("estimate", **offline.estimate(
                cues, duration, stt_model,
                facts=get_translation_profile_facts(args.profile), translator=args.translator,
                translation_model=args.gemini_model if args.translator == "gemini" else None,
                sources=sources, stt=args.stt, stt_keyterms=keyterms))
            return 0
        snapshot = offline.profile_scope(args.profile)
        engine = build_translator(args.translator, cfg, offline, progress, args.gemini_model)
        stt_report = {}
        if args.mode == "transcribe":
            # Both keys (translator above, STT here) are checked before the first paid request.
            if args.stt == "elevenlabs" and not cfg.keys.elevenlabs:
                raise Error("ElevenLabs API key is missing")
            if args.stt == "groq" and not cfg.keys.groq:
                raise Error("Groq API key is missing")
            from modules.profile_context import bind_profile_snapshot
            from modules.activity_context import bind_activity_snapshot, capture_activity_snapshot
            if args.stt == "elevenlabs":
                from elevenlabs.client import ElevenLabs
                keyterms = offline.offline_keyterms(snapshot)
                client = ElevenLabs(api_key=cfg.keys.elevenlabs, timeout=offline.OFFLINE_STT_TIMEOUT_SECONDS)
                cues = offline.transcribe_elevenlabs(args.input, duration, encoder, client, keyterms,
                                                     stt_model, progress)
                stt_report = {"stt_provider": "elevenlabs", "stt_model": stt_model, "stt_keyterms": len(keyterms)}
            else:
                from groq import Groq
                with bind_profile_snapshot(snapshot), bind_activity_snapshot(capture_activity_snapshot("")):
                    prompt = offline.stt_prompt(snapshot)
                client = Groq(api_key=cfg.keys.groq, max_retries=0, timeout=offline.OFFLINE_STT_TIMEOUT_SECONDS)
                try:
                    cues = offline.transcribe(args.input, duration, encoder, client, prompt, stt_model, progress)
                finally:
                    client.close()
                stt_report = {"stt_provider": "groq", "stt_model": stt_model}
            if not cues:
                raise Error("no usable transcription segments")
        targets, report = offline.translate(cues, snapshot, engine, progress)
        report.update(stt_report)
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
        # Only our own fixed messages reach stderr; a generic ValueError may carry provider data.
        if isinstance(exc, (OfflineSubtitleError, FileExistsError)):
            print(str(exc), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
