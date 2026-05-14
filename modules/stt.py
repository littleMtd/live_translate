import io
import queue
import threading
import time
import numpy as np
import soundfile as sf

from config import cfg
from utils.audio import rms as _rms
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from utils.text_heuristics import SENSEVOICE_NOISE_TAGS, SENSEVOICE_TAG_RE
from modules.pipeline_events import TranscriptionEvent
from modules.streamer_profiles import build_stt_glossary
from modules.stt_policy import (
    build_groq_prompt,
    is_hallucinated,
    normalize_prompt_text,
    should_reject_language,
    should_reject_segments,
)

log = get_logger("stt")

_NOISE_TAGS = SENSEVOICE_NOISE_TAGS
_TAG_RE = SENSEVOICE_TAG_RE

_CONSECUTIVE_NONE_WARN = 10   # warn after this many consecutive silent results
_SENSEVOICE_PROBE_EVERY = 50  # after this many Groq transcriptions, probe SenseVoice once
_GROQ_CONTEXT_CHARS = 120


def _is_hallucinated(text: str) -> bool:
    return is_hallucinated(text, cfg.stt.max_japanese_chars, log)


def _normalize_prompt_text(text: str, max_chars: int | None = None) -> str:
    return normalize_prompt_text(text, max_chars)


class STTEngine:
    def __init__(self):
        self._sense_voice = None
        self._groq_client = None
        self._use_groq = (cfg.stt.primary_engine == "groq")
        self._consecutive_none = 0
        self._sv_fallback_counter = 0   # counts Groq calls since SenseVoice failure
        self._last_transcript: str = ""
        if self._use_groq:
            self._init_groq()
        else:
            self._load_sense_voice()

        if self._sense_voice is None and self._groq_client is None:
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

    def _init_groq(self):
        if not cfg.keys.groq:
            log.error("GROQ_API_KEY not set — STT unavailable")
            return
        try:
            from groq import Groq
            self._groq_client = Groq(api_key=cfg.keys.groq)
            log.info("Groq whisper-large-v3 ready as STT fallback")
        except Exception as e:
            log.error("Failed to init Groq client: %s", e)

    def transcribe(self, audio: np.ndarray) -> str | None:
        event = self.transcribe_event(audio)
        return event.text if event else None

    def transcribe_event(self, audio: np.ndarray) -> TranscriptionEvent | None:
        if not self._use_groq:
            result = self._transcribe_sensevoice(audio)
            if result is not None:
                self._consecutive_none = 0
                self._last_transcript = result
                return self._event(result, "sensevoice")
            # SenseVoice failed — fall through to Groq
            self._use_groq = True
            self._init_groq()
        else:
            # Periodically probe SenseVoice recovery (mirrors translator fallback logic).
            # Only attempt if we actually loaded a SenseVoice model at some point.
            if self._sense_voice is not None:
                self._sv_fallback_counter += 1
                if self._sv_fallback_counter >= _SENSEVOICE_PROBE_EVERY:
                    self._sv_fallback_counter = 0
                    probe = self._transcribe_sensevoice(audio)
                    if probe is not None:
                        log.info("SenseVoice recovered — switching back from Groq")
                        self._use_groq = False
                        self._consecutive_none = 0
                        self._last_transcript = probe
                        return self._event(probe, "sensevoice")

        result = self._transcribe_groq(audio)
        if result is not None:
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

    @staticmethod
    def _event(text: str, engine: str) -> TranscriptionEvent:
        return TranscriptionEvent(
            text=text,
            engine=engine,
            profile_id=cfg.active_streamer_profile,
        )

    def _transcribe_sensevoice(self, audio: np.ndarray) -> str | None:
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
            return None

    def _transcribe_groq(self, audio: np.ndarray) -> str | None:
        if self._groq_client is None:
            return None
        if _rms(audio) < cfg.audio.volume_threshold:
            log.debug("Groq STT skipped: audio below volume threshold")
            return None
        try:
            buf = io.BytesIO()
            sf.write(buf, audio, cfg.audio.sample_rate, format="WAV", subtype="PCM_16")
            buf.seek(0)
            buf.name = "audio.wav"
            dynamic_prompt = self._build_groq_prompt()

            resp = self._groq_client.audio.transcriptions.create(
                model=cfg.stt.groq_model,
                file=buf,
                language=cfg.stt.language,
                prompt=dynamic_prompt,
                response_format="verbose_json",
                temperature=0.0,
            )

            # Language sanity — only reject if clearly Japanese (the dominant hallucination lang)
            detected_lang = getattr(resp, "language", None)
            if should_reject_language(detected_lang, getattr(resp, "text", "") or "", log):
                return None

            # Confidence filtering via segment metadata
            segments = getattr(resp, "segments", None) or []
            if should_reject_segments(
                segments,
                text=getattr(resp, "text", "") or "",
                no_speech_threshold=cfg.stt.no_speech_threshold,
                avg_logprob_threshold=cfg.stt.avg_logprob_threshold,
                logger=log,
            ):
                return None

            text = (getattr(resp, "text", "") or "").strip()
            if not text:
                return None
            if _is_hallucinated(text):
                return None
            log.debug("Groq: %s", text)
            return text
        except Exception as e:
            log.error("Groq STT error: %s", e)
            return None

    def _build_groq_prompt(self) -> str | None:
        return build_groq_prompt(
            seed_prompt=cfg.stt.groq_prompt,
            use_profile_glossary=cfg.stt.use_profile_glossary,
            active_profile=cfg.active_streamer_profile,
            last_transcript=self._last_transcript,
            glossary_builder=build_stt_glossary,
            max_context_chars=_GROQ_CONTEXT_CHARS,
        )


def start(audio_queue: queue.Queue, text_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        engine = STTEngine()
        while not stop_event.is_set():
            has_audio, audio = poll_queue(audio_queue, stop_event, pause_event)
            if not has_audio:
                continue

            started = time.monotonic()
            event = engine.transcribe_event(audio)
            metrics.observe_latency("stt", time.monotonic() - started)
            if event:
                metrics.increment("stt.success")
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
