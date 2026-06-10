import io
import queue
import threading
import time
from pathlib import Path
import numpy as np
import soundfile as sf

from config import cfg
from utils.audio import rms as _rms, write_wav
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import runtime_events
from utils.text_heuristics import SENSEVOICE_NOISE_TAGS, SENSEVOICE_TAG_RE
from modules.pipeline_events import TranscriptionEvent
from modules.streamer_profiles import build_stt_glossary
from modules.stt_policy import (
    build_groq_prompt_budget,
    dedupe_transcript_overlap,
    is_hallucinated,
    normalize_prompt_text,
    segment_rejection_reason,
    segment_stats,
    should_reject_language,
)

log = get_logger("stt")

_NOISE_TAGS = SENSEVOICE_NOISE_TAGS
_TAG_RE = SENSEVOICE_TAG_RE

_CONSECUTIVE_NONE_WARN = 10   # warn after this many consecutive silent results
_SENSEVOICE_PROBE_EVERY = 50  # after this many Groq transcriptions, probe SenseVoice once
_GROQ_CONTEXT_CHARS = 120
_GROQ_PROMPT_MAX_CHARS = 896
_AUDIO_DUMP_ROOT = Path(__file__).resolve().parent.parent / "logs" / "audio_dump"


def _is_groq_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    body = getattr(exc, "body", None)
    message = f"{exc} {body}".lower()
    return "rate_limit_exceeded" in message


def _is_hallucinated(text: str) -> bool:
    return is_hallucinated(text, cfg.stt.max_japanese_chars, log)


def _normalize_prompt_text(text: str, max_chars: int | None = None) -> str:
    return normalize_prompt_text(text, max_chars)


def _audio_seconds(audio: np.ndarray) -> float:
    return round(len(audio) / max(1, cfg.audio.sample_rate), 3)


def _cfg_audio_bool(name: str, default: bool) -> bool:
    value = getattr(cfg.audio, name, default)
    return bool(value) if isinstance(value, bool) else default


def _cfg_audio_float(name: str, default: float) -> float:
    value = getattr(cfg.audio, name, default)
    return float(value) if isinstance(value, (int, float)) else default


def _peak(audio: np.ndarray) -> float:
    return float(np.max(np.abs(audio))) if len(audio) else 0.0


def _normalize_audio_for_stt(audio: np.ndarray) -> tuple[np.ndarray, dict[str, float | bool]]:
    raw_rms = _rms(audio)
    raw_peak = _peak(audio)
    stats: dict[str, float | bool] = {
        "audio_rms": round(raw_rms, 6),
        "audio_peak": round(raw_peak, 6),
        "normalized_rms": round(raw_rms, 6),
        "normalized_peak": round(raw_peak, 6),
        "normalization_gain": 1.0,
        "normalization_limited": False,
    }
    if not _cfg_audio_bool("stt_normalize_enabled", False) or raw_rms <= 0:
        return audio, stats

    target_rms = max(0.0, _cfg_audio_float("stt_target_rms", 0.0))
    max_gain = max(1.0, _cfg_audio_float("stt_max_gain", 1.0))
    peak_limit = min(1.0, max(0.0, _cfg_audio_float("stt_peak_limit", 1.0)))
    if target_rms <= 0 or peak_limit <= 0:
        return audio, stats

    gain = min(max_gain, target_rms / raw_rms)
    normalized = audio.astype(np.float32, copy=True) * np.float32(gain)
    limited = False
    normalized_peak = _peak(normalized)
    if normalized_peak > peak_limit:
        normalized *= np.float32(peak_limit / normalized_peak)
        limited = True

    stats.update(
        {
            "normalized_rms": round(_rms(normalized), 6),
            "normalized_peak": round(_peak(normalized), 6),
            "normalization_gain": round(float(gain), 3),
            "normalization_limited": limited,
        }
    )
    return normalized.astype(np.float32, copy=False), stats


class STTEngine:
    def __init__(self):
        self._sense_voice = None
        self._groq_client = None
        self._groq_fallback_client = None
        self._use_groq = (cfg.stt.primary_engine == "groq")
        self._consecutive_none = 0
        self._sv_fallback_counter = 0   # counts Groq calls since SenseVoice failure
        self._groq_rate_limited_until = 0.0
        self._groq_fallback_rate_limited_until = 0.0
        # Once the primary key hits 429, keep preferring the fallback key even
        # after primary cooldown. We switch back only if the fallback key also
        # rate-limits, which avoids burning primary quota during live capture.
        self._groq_prefer_fallback_key = False
        self._last_transcript: str = ""
        self._last_avg_logprob: float | None = None
        self._last_no_speech_prob: float | None = None
        self._last_sensevoice_error = False
        # Monotonic per-transcription counter; minted once per transcribe_event
        # call (single STT thread, so no lock needed) to correlate downstream events.
        self._utterance_seq = 0
        self._current_utterance_id = ""
        self._last_audio_seconds = 0.0
        self._last_prompt_budget = None
        if self._use_groq:
            self._init_groq()
            self._init_groq_fallback()
        else:
            self._load_sense_voice()

        self.available = (
            self._sense_voice is not None
            or self._groq_client is not None
            or self._groq_fallback_client is not None
        )
        if not self.available:
            log.error("STT unavailable: both SenseVoice and Groq failed to initialize")

    def _load_sense_voice(self):
        try:
            from funasr import AutoModel
            log.info("Loading SenseVoice-Small…")
            self._sense_voice = AutoModel(
                model=cfg.stt.sensevoice_model,
                trust_remote_code=True,
                device=cfg.stt.sensevoice_device,
            )
            log.info("SenseVoice-Small loaded")
        except Exception as e:
            log.error("Failed to load SenseVoice-Small: %s — will use Groq", e)
            self._use_groq = True
            self._init_groq()
            self._init_groq_fallback()

    def _init_groq(self):
        if not cfg.keys.groq:
            log.error("GROQ_API_KEY not set — STT unavailable")
            return
        try:
            from groq import Groq
            self._groq_client = Groq(
                api_key=cfg.keys.groq,
                max_retries=cfg.stt.groq_max_retries,
                timeout=cfg.stt.groq_timeout,
            )
            log.info(
                "Groq %s ready as STT fallback (timeout=%ss, max_retries=%s)",
                cfg.stt.groq_model,
                cfg.stt.groq_timeout,
                cfg.stt.groq_max_retries,
            )
        except Exception as e:
            log.error("Failed to init Groq client: %s", e)

    def _init_groq_fallback(self):
        if not cfg.keys.groq_fallback:
            return
        try:
            from groq import Groq
            self._groq_fallback_client = Groq(
                api_key=cfg.keys.groq_fallback,
                max_retries=cfg.stt.groq_max_retries,
                timeout=cfg.stt.groq_timeout,
            )
            log.info("Groq fallback key ready (will activate when primary is rate-limited)")
        except Exception as e:
            log.error("Failed to init Groq fallback client: %s", e)

    def transcribe(self, audio: np.ndarray) -> str | None:
        event = self.transcribe_event(audio)
        return event.text if event else None

    def transcribe_event(self, audio: np.ndarray) -> TranscriptionEvent | None:
        self._utterance_seq += 1
        self._current_utterance_id = f"utt-{self._utterance_seq}"
        self._last_audio_seconds = _audio_seconds(audio)
        if not self._use_groq:
            result = self._transcribe_sensevoice(audio)
            if result is not None:
                result = dedupe_transcript_overlap(self._last_transcript, result)
                if not result:
                    return None
                self._consecutive_none = 0
                self._last_transcript = result
                return self._event(result, "sensevoice")
            if not self._last_sensevoice_error:
                return None
            # SenseVoice engine failed — fall through to Groq
            self._use_groq = True
            self._init_groq()
            self._init_groq_fallback()
        else:
            # Periodically probe SenseVoice recovery (mirrors translator fallback logic).
            # Only attempt if we actually loaded a SenseVoice model at some point.
            if self._sense_voice is not None:
                self._sv_fallback_counter += 1
                if self._sv_fallback_counter >= _SENSEVOICE_PROBE_EVERY:
                    self._sv_fallback_counter = 0
                    probe = self._transcribe_sensevoice(audio)
                    if probe is not None:
                        probe = dedupe_transcript_overlap(self._last_transcript, probe)
                        if not probe:
                            return None
                        log.info("SenseVoice recovered — switching back from Groq")
                        self._use_groq = False
                        self._consecutive_none = 0
                        self._last_transcript = probe
                        return self._event(probe, "sensevoice")

        result = self._transcribe_groq(audio)
        if result is not None:
            result = dedupe_transcript_overlap(self._last_transcript, result)
            if not result:
                return None
            self._consecutive_none = 0
            self._last_transcript = result
            return self._event(result, "groq")
        else:
            self._consecutive_none += 1
            if self._consecutive_none == _CONSECUTIVE_NONE_WARN:
                log.warning(
                    "STT returned None %d times in a row — both engines may be down",
                    _CONSECUTIVE_NONE_WARN,
                )
        return None

    def _event(self, text: str, engine: str) -> TranscriptionEvent:
        return TranscriptionEvent(
            text=text,
            engine=engine,
            profile_id=cfg.active_streamer_profile,
            utterance_id=self._current_utterance_id,
            audio_seconds=self._last_audio_seconds,
            avg_logprob=self._last_avg_logprob,
            no_speech_prob=self._last_no_speech_prob,
        )

    def _transcribe_sensevoice(self, audio: np.ndarray) -> str | None:
        self._last_avg_logprob = None
        self._last_no_speech_prob = None
        self._last_sensevoice_error = False
        try:
            res = self._sense_voice.generate(
                input=audio,
                cache={},
                language=cfg.stt.language,
                use_itn=True,
                batch_size_s=cfg.stt.batch_size_s,
            )
            text = res[0]["text"] if res else ""
            # Reject pure noise chunks (no speech tag present)
            if not any(tag in text for tag in ("<|Speech|>", "<|WITHITN|>", "<|withitn|>")):
                if any(tag in text for tag in _NOISE_TAGS):
                    return None
            # Strip ALL metadata tokens: <|ko|>, <|EMO_UNKNOWN|>, <|Speech|>, etc.
            text = _TAG_RE.sub("", text).strip()
            if not text:
                return None
            log.debug("SenseVoice: %s", text)
            return text
        except Exception as e:
            log.error("SenseVoice error: %s", e)
            self._last_sensevoice_error = True
            return None

    def _transcribe_groq(self, audio: np.ndarray) -> str | None:
        started = time.monotonic()
        self._last_avg_logprob = None
        self._last_no_speech_prob = None
        primary_ready = self._groq_client is not None and started >= self._groq_rate_limited_until
        fallback_ready = (
            self._groq_fallback_client is not None
            and started >= self._groq_fallback_rate_limited_until
        )
        prefer_fallback = bool(getattr(self, "_groq_prefer_fallback_key", False))
        if prefer_fallback and fallback_ready:
            active_client = self._groq_fallback_client
            using_fallback = True
            log.debug("Using preferred Groq fallback key")
        elif primary_ready:
            active_client = self._groq_client
            using_fallback = False
        elif fallback_ready:
            active_client = self._groq_fallback_client
            using_fallback = True
            self._groq_prefer_fallback_key = True
            log.debug("Primary Groq key rate-limited; using fallback key")
        else:
            if self._groq_client is None and self._groq_fallback_client is None:
                reason = "no_client"
            else:
                reason = "rate_limit_cooldown"
                remaining = round(
                    min(
                        self._groq_rate_limited_until if self._groq_client else 0,
                        self._groq_fallback_rate_limited_until if self._groq_fallback_client else 0,
                    ) - started,
                    2,
                )
                log.debug("Both Groq keys rate-limited (%.2fs left on shorter cooldown)", remaining)
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="skipped",
                reason=reason,
                request_sent=False,
            )
            return None
        request_audio, audio_stats = _normalize_audio_for_stt(audio)
        gate_rms = _rms(request_audio)
        if gate_rms < cfg.audio.volume_threshold:
            self._last_avg_logprob = None
            self._last_no_speech_prob = None
            log.debug("Groq STT skipped: audio below volume threshold")
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="skipped",
                reason="below_volume_threshold",
                request_sent=False,
                audio_stats=audio_stats,
            )
            return None
        request_sent = False
        try:
            buf = io.BytesIO()
            sf.write(buf, request_audio, cfg.audio.sample_rate, format="WAV", subtype="PCM_16")
            buf.seek(0)
            buf.name = "audio.wav"
            dynamic_prompt = self._build_groq_prompt()

            request_sent = True
            resp = active_client.audio.transcriptions.create(
                model=cfg.stt.groq_model,
                file=buf,
                language=cfg.stt.language,
                prompt=dynamic_prompt,
                response_format="verbose_json",
                temperature=0.0,
            )

            # Language sanity — only reject if clearly Japanese (the dominant hallucination lang)
            detected_lang = getattr(resp, "language", None)
            text = (getattr(resp, "text", "") or "").strip()
            if should_reject_language(detected_lang, text, log):
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="language",
                    request_sent=request_sent,
                    text=text,
                    audio_stats=audio_stats,
                )
                return None

            # Confidence filtering via segment metadata
            segments = getattr(resp, "segments", None) or []
            reason, stats = segment_rejection_reason(
                segments,
                text=text,
                no_speech_threshold=cfg.stt.no_speech_threshold,
                avg_logprob_threshold=cfg.stt.avg_logprob_threshold,
                logger=log,
            )
            if reason is not None:
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason=reason or "segments",
                    request_sent=request_sent,
                    text=text,
                    avg_logprob=stats.logprob if stats else None,
                    no_speech_prob=stats.no_speech if stats else None,
                    audio_stats=audio_stats,
                )
                return None

            if not text:
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="empty",
                    request_sent=request_sent,
                    text=text,
                    audio_stats=audio_stats,
                )
                return None
            if _is_hallucinated(text):
                self._last_avg_logprob = None
                self._last_no_speech_prob = None
                self._emit_stt_runtime_event(
                    audio=audio,
                    started=started,
                    status="filtered",
                    reason="hallucinated",
                    request_sent=request_sent,
                    text=text,
                    audio_stats=audio_stats,
                )
                return None
            log.debug("Groq: %s", text)
            stats = segment_stats(segments)
            self._last_avg_logprob = stats.logprob if stats else None
            self._last_no_speech_prob = stats.no_speech if stats else None
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="success",
                reason="",
                request_sent=request_sent,
                text=text,
                avg_logprob=stats.logprob if stats else None,
                no_speech_prob=stats.no_speech if stats else None,
                audio_stats=audio_stats,
            )
            return text
        except Exception as e:
            self._last_avg_logprob = None
            self._last_no_speech_prob = None
            if _is_groq_rate_limit_error(e):
                reason = "rate_limited"
                cooldown_end = time.monotonic() + max(0.0, cfg.stt.groq_rate_limit_cooldown_sec)
                if using_fallback:
                    self._groq_fallback_rate_limited_until = cooldown_end
                    self._groq_prefer_fallback_key = False
                    if (
                        self._groq_client is not None
                        and time.monotonic() >= self._groq_rate_limited_until
                    ):
                        log.warning(
                            "Groq STT fallback key rate limited; retrying same chunk with primary key: %s",
                            e,
                        )
                        self._emit_stt_runtime_event(
                            audio=audio,
                            started=started,
                            status="failed",
                            reason=reason,
                            request_sent=request_sent,
                            audio_stats=audio_stats,
                        )
                        return self._transcribe_groq(audio)
                    log.warning("Groq STT fallback key rate limited; dropping chunk: %s", e)
                else:
                    self._groq_rate_limited_until = cooldown_end
                    if self._groq_fallback_client is not None:
                        self._groq_prefer_fallback_key = True
                    if (
                        self._groq_fallback_client is not None
                        and time.monotonic() >= self._groq_fallback_rate_limited_until
                    ):
                        log.warning(
                            "Groq STT primary key rate limited; retrying same chunk with fallback key: %s",
                            e,
                        )
                        self._emit_stt_runtime_event(
                            audio=audio,
                            started=started,
                            status="failed",
                            reason=reason,
                            request_sent=request_sent,
                            audio_stats=audio_stats,
                        )
                        return self._transcribe_groq(audio)
                    log.warning("Groq STT rate limited; dropping chunk without fallback retry: %s", e)
            else:
                log.error("Groq STT error: %s", e)
                reason = "error"
            self._emit_stt_runtime_event(
                audio=audio,
                started=started,
                status="failed",
                reason=reason,
                request_sent=request_sent,
                audio_stats=audio_stats,
            )
            return None

    def _build_groq_prompt(self) -> str | None:
        budget = build_groq_prompt_budget(
            seed_prompt=cfg.stt.groq_prompt,
            use_profile_glossary=cfg.stt.use_profile_glossary,
            active_profile=cfg.active_streamer_profile,
            last_transcript=self._last_transcript,
            glossary_builder=build_stt_glossary,
            max_context_chars=_GROQ_CONTEXT_CHARS,
            max_prompt_chars=_GROQ_PROMPT_MAX_CHARS,
        )
        self._last_prompt_budget = budget
        return budget.prompt

    def _emit_stt_runtime_event(
        self,
        *,
        audio: np.ndarray,
        started: float,
        status: str,
        reason: str,
        request_sent: bool,
        text: str = "",
        avg_logprob: float | None = None,
        no_speech_prob: float | None = None,
        audio_stats: dict[str, float | bool] | None = None,
    ) -> None:
        audio_stats = audio_stats or {}
        # Prompt budget only reflects this request; on skipped paths no prompt
        # was built, so the (possibly stale) budget is intentionally omitted.
        budget = getattr(self, "_last_prompt_budget", None) if request_sent else None
        runtime_events.emit(
            "stt",
            utterance_id=getattr(self, "_current_utterance_id", ""),
            engine="groq",
            model=cfg.stt.groq_model,
            status=status,
            reason=reason,
            request_sent=request_sent,
            audio_seconds=_audio_seconds(audio),
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            text_len=len(text or ""),
            profile_id=cfg.active_streamer_profile,
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            audio_rms=audio_stats.get("audio_rms"),
            audio_peak=audio_stats.get("audio_peak"),
            normalized_rms=audio_stats.get("normalized_rms"),
            normalized_peak=audio_stats.get("normalized_peak"),
            normalization_gain=audio_stats.get("normalization_gain"),
            normalization_limited=audio_stats.get("normalization_limited"),
            prompt_bytes=budget.prompt_bytes if budget else None,
            prompt_max_bytes=budget.max_prompt_bytes if budget else None,
            glossary_present=budget.glossary_present if budget else None,
            glossary_truncated=budget.glossary_truncated if budget else None,
            context_present=budget.context_present if budget else None,
            context_included=budget.context_included if budget else None,
        )


def start(audio_queue: queue.Queue, text_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        engine = STTEngine()
        if not engine.available:
            # Both SenseVoice and Groq failed to load. Don't sit and spin
            # consuming audio chunks that we can never transcribe — signal
            # shutdown so main.py can tear down the rest of the pipeline.
            log.error("STT thread aborting: no engine available")
            stop_event.set()
            return
        # Collection-mode audio dump: one session dir per run so per-run
        # utterance ids (utt-1, utt-2…) don't collide across restarts. Keyed by
        # runtime_events.run_id (timestamp-pid) so two restarts in the same
        # second land in different dirs AND the dir matches the run_id stamped
        # on every runtime event, making audio joinable from the logs.
        dump_dir: Path | None = None
        if cfg.stt.dump_audio:
            dump_dir = _AUDIO_DUMP_ROOT / runtime_events.run_id
            log.info("STT audio dump enabled → %s", dump_dir)
        while not stop_event.is_set():
            has_audio, audio = poll_queue(audio_queue, stop_event, pause_event)
            if not has_audio:
                continue

            started = time.monotonic()
            event = engine.transcribe_event(audio)
            metrics.observe_latency("stt", time.monotonic() - started)
            if event:
                metrics.increment("stt.success")
                if dump_dir is not None and event.utterance_id:
                    try:
                        write_wav(dump_dir / f"{event.utterance_id}.wav", audio, cfg.audio.sample_rate)
                    except Exception as exc:  # never let dumping break transcription
                        log.warning("Audio dump failed for %s: %s", event.utterance_id, exc)
                put_latest(text_queue, event, log, "text_queue", "tokens")
            else:
                metrics.increment("stt.none")
            metrics.log_summary_if_due()

        log.info("STT stopped")

    return start_daemon_thread("STT", run)


if __name__ == "__main__":
    import time, sounddevice as sd

    engine = STTEngine()
    print(f"Recording {cfg.audio.chunk_seconds}s…")
    audio = sd.rec(
        int(cfg.audio.sample_rate * cfg.audio.chunk_seconds),
        samplerate=cfg.audio.sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    result = engine.transcribe(audio[:, 0])
    print("Transcription:", result)
