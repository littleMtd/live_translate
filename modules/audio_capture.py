import queue
import threading
import numpy as np
import sounddevice as sd

from config import cfg
from utils.logger import get_logger
from utils.queue_utils import drain_put

log = get_logger("audio_capture")

_FIXED_BUF_MAX_SAMPLES = 8 * cfg.audio.sample_rate   # 8 seconds


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2)))


# ---------------------------------------------------------------------------
# Silero VAD
# ---------------------------------------------------------------------------

class _SileroDetector:
    """
    Wraps the Silero VAD model for per-frame speech detection.
    Requires 16 kHz mono audio. Silero window = 512 samples (32 ms).
    """
    _WINDOW = 512   # samples @ 16 kHz

    def __init__(self, threshold: float):
        import torch
        self._threshold = threshold
        self._sr        = cfg.audio.sample_rate
        model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            verbose=False,
        )
        model.eval()
        self._model = model

    def is_speech(self, frame: np.ndarray) -> bool:
        """Return True if any 32 ms window in frame has speech prob >= threshold."""
        import torch
        for start in range(0, len(frame) - self._WINDOW + 1, self._WINDOW):
            tensor = torch.from_numpy(frame[start : start + self._WINDOW]).float()
            if self._model(tensor, self._sr).item() >= self._threshold:
                return True
        return False

    def reset(self) -> None:
        """Reset LSTM hidden states between utterances."""
        self._model.reset_states()


def _load_silero(threshold: float) -> "_SileroDetector | None":
    """Try to load Silero VAD. Returns None and falls back to RMS on any failure."""
    try:
        log.info("Loading Silero VAD (first run downloads ~2 MB)…")
        det = _SileroDetector(threshold)
        log.info("Silero VAD ready (threshold=%.2f)", threshold)
        return det
    except Exception as exc:
        log.warning("Silero VAD unavailable: %s — falling back to RMS threshold", exc)
        return None


# ---------------------------------------------------------------------------
# VAD state machine
# ---------------------------------------------------------------------------

class _VadState:
    """Accumulates audio frames and emits chunks at speech/silence boundaries."""

    def __init__(self, audio_queue: queue.Queue,
                 silero: "_SileroDetector | None" = None):
        self._q              = audio_queue
        self._silero         = silero
        self._buf: list      = []
        self._total_samples  = 0
        self._speech_samples = 0
        self._silent_samples = 0

        sr = cfg.audio.sample_rate
        self._silence_gate     = int(cfg.audio.vad_silence_sec    * sr)
        self._min_speech       = int(cfg.audio.vad_min_speech_sec * sr)
        self._max_speech       = int(cfg.audio.vad_max_speech_sec * sr)
        self._volume_threshold = cfg.audio.volume_threshold

        mode = "Silero" if silero is not None else "RMS"
        log.info("_VadState init — mode=%s silence=%.1fs min_speech=%.1fs max=%.1fs",
                 mode, cfg.audio.vad_silence_sec,
                 cfg.audio.vad_min_speech_sec, cfg.audio.vad_max_speech_sec)

    def push(self, frame: np.ndarray) -> None:
        self._buf.append(frame.copy())
        self._total_samples += len(frame)

        if self._silero is not None:
            is_speech = self._silero.is_speech(frame)
        else:
            is_speech = _rms(frame) >= self._volume_threshold

        if is_speech:
            self._speech_samples += len(frame)
            self._silent_samples  = 0
        else:
            self._silent_samples += len(frame)

        silence_hit = self._silent_samples >= self._silence_gate
        max_hit     = self._total_samples  >= self._max_speech

        if (silence_hit or max_hit) and self._speech_samples >= self._min_speech:
            self._emit()
        elif max_hit:
            # Mostly silence at max length — discard
            self._reset()

    def _emit(self) -> None:
        chunk = np.concatenate(self._buf)
        self._reset()
        drained = drain_put(self._q, chunk)
        if drained:
            log.warning("audio_queue backlog cleared (%d chunks), keeping latest", drained)
        else:
            log.debug("VAD chunk emitted: %.2fs", len(chunk) / cfg.audio.sample_rate)

    def _reset(self) -> None:
        self._buf            = []
        self._total_samples  = 0
        self._speech_samples = 0
        self._silent_samples = 0
        if self._silero is not None:
            self._silero.reset()


# ---------------------------------------------------------------------------
# Public start()
# ---------------------------------------------------------------------------

def start(audio_queue: queue.Queue, stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        silero = None
        if cfg.audio.vad_enabled:
            silero = _load_silero(cfg.audio.vad_silero_threshold)

        vad           = _VadState(audio_queue, silero) if cfg.audio.vad_enabled else None
        fixed_buf     = np.zeros(0, dtype=np.float32)
        chunk_samples = cfg.audio.sample_rate * cfg.audio.chunk_seconds

        def callback(indata, frames, time_info, status):
            nonlocal fixed_buf
            if status:
                log.warning("sounddevice status: %s", status)
            if pause_event and pause_event.is_set():
                return
            mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()

            if vad:
                vad.push(mono)
            else:
                fixed_buf = np.concatenate([fixed_buf, mono])
                if len(fixed_buf) > _FIXED_BUF_MAX_SAMPLES:
                    excess    = len(fixed_buf) - _FIXED_BUF_MAX_SAMPLES
                    fixed_buf = fixed_buf[excess:]
                    log.warning("fixed_buf exceeded max (%ds) — oldest %dms discarded",
                                _FIXED_BUF_MAX_SAMPLES // cfg.audio.sample_rate,
                                round(excess / cfg.audio.sample_rate * 1000))
                while len(fixed_buf) >= chunk_samples:
                    chunk      = fixed_buf[:chunk_samples]
                    fixed_buf  = fixed_buf[chunk_samples:]
                    if _rms(chunk) < cfg.audio.volume_threshold:
                        log.debug("Silence detected, skipping chunk")
                        continue
                    drained = drain_put(audio_queue, chunk.copy())
                    if drained:
                        log.warning("audio_queue backlog cleared (%d chunks), keeping latest",
                                    drained)

        mode = "VAD" if cfg.audio.vad_enabled else f"fixed {cfg.audio.chunk_seconds}s"
        log.info("Starting WASAPI loopback capture — mode=%s samplerate=%d",
                 mode, cfg.audio.sample_rate)

        with sd.InputStream(
            samplerate=cfg.audio.sample_rate,
            channels=cfg.audio.channels,
            dtype="float32",
            blocksize=cfg.audio.sample_rate // 10,   # 100 ms frames
            callback=callback,
            device=_find_loopback_device(),
        ):
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)

        log.info("Audio capture stopped")

    t = threading.Thread(target=run, name="AudioCapture", daemon=True)
    t.start()
    return t


def _find_loopback_device() -> int | None:
    devices = sd.query_devices()

    if cfg.audio.device_name:
        name_lower = cfg.audio.device_name.lower()
        for i, dev in enumerate(devices):
            if name_lower in dev["name"].lower():
                log.info("Using configured device [%d]: %s", i, dev["name"])
                return i
        log.warning("Configured device_name %r not found, falling back to auto-detect",
                    cfg.audio.device_name)

    for i, dev in enumerate(devices):
        name = dev["name"].lower()
        if "loopback" in name or "stereo mix" in name or "立體聲混音" in name:
            log.info("Auto-detected loopback device [%d]: %s", i, dev["name"])
            return i

    raise RuntimeError(
        "No loopback audio device found. "
        "Install VB-Cable or enable 'Stereo Mix', then restart. "
        "Run 'python modules/audio_capture.py' to list available devices."
    )


if __name__ == "__main__":
    import time
    print("Available audio devices:")
    print(sd.query_devices())
    print(f"\nWill use device: {_find_loopback_device()}")
    print(f"VAD enabled: {cfg.audio.vad_enabled}")
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = start(q, stop)
    print("Recording for 15 seconds…")
    time.sleep(15)
    stop.set()
    t.join()
    chunks = []
    while not q.empty():
        chunks.append(q.get())
    print(f"Captured {len(chunks)} chunks")
    for i, c in enumerate(chunks):
        print(f"  [{i+1}] {len(c)/cfg.audio.sample_rate:.2f}s  rms={_rms(c):.4f}")
