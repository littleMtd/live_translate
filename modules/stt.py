import io
import queue
import re
import threading
import numpy as np
import soundfile as sf


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2)))

from config import cfg
from utils.logger import get_logger
from utils.queue_utils import drain_put

log = get_logger("stt")

# Background sound tags that SenseVoice emits — not speech
_NOISE_TAGS = {"<|BGM|>", "<|Applause|>", "<|Laughter|>",
               "<|Cry|>", "<|Sneeze|>", "<|Breath|>", "<|Cough|>"}

# Strip ALL SenseVoice metadata tokens: <|ko|>, <|EMO_UNKNOWN|>, <|Speech|>, etc.
_TAG_RE = re.compile(r'<\|[^|]*\|>')

_CONSECUTIVE_NONE_WARN = 10   # warn after this many consecutive silent results
_SENSEVOICE_PROBE_EVERY = 50  # after this many Groq transcriptions, probe SenseVoice once


class STTEngine:
    def __init__(self):
        self._sense_voice = None
        self._groq_client = None
        self._use_groq = (cfg.stt.primary_engine == "groq")
        self._consecutive_none = 0
        self._sv_fallback_counter = 0   # counts Groq calls since SenseVoice failure
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
        if not self._use_groq:
            result = self._transcribe_sensevoice(audio)
            if result is not None:
                self._consecutive_none = 0
                return result
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
                        return probe

        result = self._transcribe_groq(audio)
        if result is not None:
            self._consecutive_none = 0
        else:
            self._consecutive_none += 1
            if self._consecutive_none == _CONSECUTIVE_NONE_WARN:
                log.warning(
                    "STT returned None %d times in a row — both engines may be down",
                    _CONSECUTIVE_NONE_WARN,
                )
        return result

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

            resp = self._groq_client.audio.transcriptions.create(
                model=cfg.stt.groq_model,
                file=buf,
                language=cfg.stt.language,
                prompt=cfg.stt.groq_prompt or None,
                response_format="text",
            )
            text = resp.strip() if resp else ""
            if not text:
                return None
            log.debug("Groq: %s", text)
            return text
        except Exception as e:
            log.error("Groq STT error: %s", e)
            return None


def start(audio_queue: queue.Queue, text_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        engine = STTEngine()
        while not stop_event.is_set():
            if pause_event and pause_event.is_set():
                stop_event.wait(timeout=0.2)
                continue
            try:
                audio = audio_queue.get(timeout=1)
            except queue.Empty:
                continue

            text = engine.transcribe(audio)
            if text:
                drained = drain_put(text_queue, text)
                if drained:
                    log.warning("text_queue backlog cleared (%d tokens), keeping latest", drained)

        log.info("STT stopped")

    t = threading.Thread(target=run, name="STT", daemon=True)
    t.start()
    return t


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
