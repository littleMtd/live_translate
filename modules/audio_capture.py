import queue
import threading
import numpy as np
import sounddevice as sd

from config import cfg
from utils.audio import rms as _rms
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import runtime_events

log = get_logger("audio_capture")

_FIXED_BUF_MAX_SAMPLES = 8 * cfg.audio.sample_rate   # 8 seconds


def _cfg_float(name: str, default: float) -> float:
    value = getattr(cfg.audio, name, default)
    return float(value) if isinstance(value, (int, float)) else default


def _cfg_int(name: str, default: int) -> int:
    value = getattr(cfg.audio, name, default)
    return int(value) if isinstance(value, int) else default


def _cfg_bool(name: str, default: bool) -> bool:
    value = getattr(cfg.audio, name, default)
    return bool(value) if isinstance(value, bool) else default


def _downmix_to_mono(indata: np.ndarray) -> np.ndarray:
    """Return float32 mono audio, averaging all captured channels."""
    if indata.ndim <= 1:
        return indata.flatten().astype(np.float32, copy=False)
    if indata.shape[1] == 1:
        return indata[:, 0].astype(np.float32, copy=False)
    return np.mean(indata, axis=1, dtype=np.float32)


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
        self._pending_overlap = np.zeros(0, dtype=np.float32)
        self._adaptive_segments_remaining = 0

        sr = cfg.audio.sample_rate
        self._silence_gate     = int(cfg.audio.vad_silence_sec    * sr)
        self._min_speech       = int(cfg.audio.vad_min_speech_sec * sr)
        self._max_speech       = int(cfg.audio.vad_max_speech_sec * sr)
        hard_max_sec = getattr(cfg.audio, "vad_hard_max_speech_sec", cfg.audio.vad_max_speech_sec)
        self._hard_max_speech  = int(max(cfg.audio.vad_max_speech_sec, hard_max_sec) * sr)
        self._overlap_samples  = int(max(0.0, getattr(cfg.audio, "vad_overlap_sec", 0.0)) * sr)
        self._silence_overlap_samples = int(max(0.0, _cfg_float("vad_silence_overlap_sec", 0.0)) * sr)
        self._adaptive_enabled = _cfg_bool("vad_adaptive_enabled", False)
        self._adaptive_segments_after_boundary = max(
            0,
            _cfg_int("vad_adaptive_after_boundary_cuts", 1),
        )
        adaptive_silence_sec = max(
            cfg.audio.vad_silence_sec,
            _cfg_float("vad_adaptive_silence_sec", cfg.audio.vad_silence_sec),
        )
        adaptive_max_sec = max(
            cfg.audio.vad_max_speech_sec,
            _cfg_float("vad_adaptive_max_speech_sec", cfg.audio.vad_max_speech_sec),
        )
        adaptive_hard_sec = max(
            hard_max_sec,
            _cfg_float("vad_adaptive_hard_max_speech_sec", hard_max_sec),
        )
        self._adaptive_silence_gate = int(adaptive_silence_sec * sr)
        self._adaptive_max_speech = int(adaptive_max_sec * sr)
        self._adaptive_hard_max_speech = int(adaptive_hard_sec * sr)
        self._adaptive_overlap_samples = int(
            max(
                self._overlap_samples / sr,
                _cfg_float("vad_adaptive_overlap_sec", self._overlap_samples / sr),
            )
            * sr
        )
        self._volume_threshold = cfg.audio.volume_threshold

        mode = "Silero" if silero is not None else "RMS"
        log.info(
            "_VadState init — mode=%s silence=%.1fs min_speech=%.1fs max=%.1fs hard_max=%.1fs "
            "overlap=%.1fs silence_overlap=%.1fs adaptive=%s",
            mode, cfg.audio.vad_silence_sec,
            cfg.audio.vad_min_speech_sec, cfg.audio.vad_max_speech_sec,
            hard_max_sec, self._overlap_samples / sr,
            self._silence_overlap_samples / sr, self._adaptive_enabled)

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

        silence_gate, max_speech, hard_max_speech, overlap_samples = self._active_limits()
        silence_hit = self._silent_samples >= silence_gate
        soft_max_hit = self._total_samples >= max_speech
        hard_max_hit = self._total_samples >= hard_max_speech
        max_hit = hard_max_hit or (soft_max_hit and self._silent_samples > 0)

        if (silence_hit or max_hit) and self._speech_samples >= self._min_speech:
            if hard_max_hit:
                cut_reason = "hard_max"
            elif max_hit:
                cut_reason = "soft_max_pause"
            else:
                cut_reason = "silence"
            next_overlap = overlap_samples if cut_reason != "silence" else self._silence_overlap_samples
            self._emit(cut_reason=cut_reason, next_overlap_samples=next_overlap)
        elif hard_max_hit:
            metrics.increment("audio.cut.discard_hard_max_no_speech")
            self._emit_vad_runtime_event(
                cut_reason="discard_hard_max_no_speech",
                audio_seconds=self._total_samples / cfg.audio.sample_rate,
                raw_audio_seconds=self._total_samples / cfg.audio.sample_rate,
                overlap_seconds=0.0,
                adaptive_active=self._adaptive_active(),
                speech_seconds=self._speech_samples / cfg.audio.sample_rate,
                silence_seconds=self._silent_samples / cfg.audio.sample_rate,
                rms_value=0.0,
                peak_value=0.0,
                queue_drained=0,
            )
            # Mostly silence at max length — discard
            self._reset(clear_overlap=True)

    def _active_limits(self) -> tuple[int, int, int, int]:
        if not self._adaptive_active():
            return (
                self._silence_gate,
                self._max_speech,
                self._hard_max_speech,
                self._overlap_samples,
            )
        return (
            self._adaptive_silence_gate,
            self._adaptive_max_speech,
            self._adaptive_hard_max_speech,
            self._adaptive_overlap_samples,
        )

    def _adaptive_active(self) -> bool:
        return self._adaptive_enabled and self._adaptive_segments_remaining > 0

    def _update_adaptive_state(self, cut_reason: str, was_adaptive: bool) -> None:
        if not self._adaptive_enabled:
            return
        if cut_reason in {"soft_max_pause", "hard_max"}:
            self._adaptive_segments_remaining = self._adaptive_segments_after_boundary
            return
        if was_adaptive:
            self._adaptive_segments_remaining = max(0, self._adaptive_segments_remaining - 1)

    def _next_overlap(self, raw_chunk: np.ndarray, cut_reason: str, samples: int) -> np.ndarray:
        if samples <= 0:
            return np.zeros(0, dtype=np.float32)
        source = raw_chunk
        if cut_reason == "silence" and 0 < self._silent_samples < len(raw_chunk):
            source = raw_chunk[: -self._silent_samples]
        if len(source) < samples:
            return np.zeros(0, dtype=np.float32)
        return source[-samples:].copy()

    def _emit(self, *, cut_reason: str, next_overlap_samples: int = 0) -> None:
        was_adaptive = self._adaptive_active()
        raw_total_samples = self._total_samples
        speech_samples = self._speech_samples
        silent_samples = self._silent_samples
        raw_chunk = np.concatenate(self._buf)
        if self._pending_overlap.size:
            chunk = np.concatenate([self._pending_overlap, raw_chunk])
        else:
            chunk = raw_chunk
        next_overlap = self._next_overlap(raw_chunk, cut_reason, next_overlap_samples)
        self._reset()
        self._pending_overlap = next_overlap
        self._update_adaptive_state(cut_reason, was_adaptive)
        metrics.increment("audio.chunks")
        metrics.increment(f"audio.cut.{cut_reason}")
        if was_adaptive:
            metrics.increment("audio.vad.adaptive")
        drained = put_latest(self._q, chunk, log, "audio_queue", "chunks")
        rms_value = _rms(chunk)
        peak_value = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
        overlap_seconds = len(chunk) - raw_total_samples
        overlap_seconds = max(0, overlap_seconds) / cfg.audio.sample_rate
        self._emit_vad_runtime_event(
            cut_reason=cut_reason,
            audio_seconds=len(chunk) / cfg.audio.sample_rate,
            raw_audio_seconds=raw_total_samples / cfg.audio.sample_rate,
            overlap_seconds=overlap_seconds,
            adaptive_active=was_adaptive,
            speech_seconds=speech_samples / cfg.audio.sample_rate,
            silence_seconds=silent_samples / cfg.audio.sample_rate,
            rms_value=rms_value,
            peak_value=peak_value,
            queue_drained=drained,
        )
        if drained == 0:
            log.debug(
                "VAD chunk emitted: %.2fs (reason=%s adaptive=%s next_overlap=%.2fs)",
                len(chunk) / cfg.audio.sample_rate,
                cut_reason,
                was_adaptive,
                len(next_overlap) / cfg.audio.sample_rate,
            )

    @staticmethod
    def _emit_vad_runtime_event(
        *,
        cut_reason: str,
        audio_seconds: float,
        raw_audio_seconds: float,
        overlap_seconds: float,
        adaptive_active: bool,
        speech_seconds: float,
        silence_seconds: float,
        rms_value: float,
        peak_value: float,
        queue_drained: int,
    ) -> None:
        runtime_events.emit(
            "audio",
            stage="vad",
            cut_reason=cut_reason,
            audio_seconds=round(audio_seconds, 3),
            raw_audio_seconds=round(raw_audio_seconds, 3),
            overlap_seconds=round(overlap_seconds, 3),
            adaptive_active=adaptive_active,
            speech_seconds=round(speech_seconds, 3),
            silence_seconds=round(silence_seconds, 3),
            rms=round(rms_value, 6),
            peak=round(peak_value, 6),
            queue_drained=queue_drained,
        )

    def _reset(self, *, clear_overlap: bool = False) -> None:
        self._buf            = []
        self._total_samples  = 0
        self._speech_samples = 0
        self._silent_samples = 0
        if clear_overlap:
            self._pending_overlap = np.zeros(0, dtype=np.float32)
        if self._silero is not None:
            self._silero.reset()


# ---------------------------------------------------------------------------
# Public start()
# ---------------------------------------------------------------------------

def start(audio_queue: queue.Queue, stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    # Resolve the loopback device synchronously so a missing device fails fast
    # in the caller's thread instead of silently killing a daemon thread.
    device = _find_loopback_device()

    def run():
        try:
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
                mono = _downmix_to_mono(indata)

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
                        metrics.increment("audio.chunks")
                        put_latest(audio_queue, chunk.copy(), log, "audio_queue", "chunks")

            mode = "VAD" if cfg.audio.vad_enabled else f"fixed {cfg.audio.chunk_seconds}s"
            capture_channels = getattr(cfg.audio, "capture_channels", cfg.audio.channels)
            log.info(
                "Starting WASAPI loopback capture — mode=%s samplerate=%d capture_channels=%d",
                mode,
                cfg.audio.sample_rate,
                capture_channels,
            )

            with sd.InputStream(
                samplerate=cfg.audio.sample_rate,
                channels=capture_channels,
                dtype="float32",
                blocksize=cfg.audio.sample_rate // 10,   # 100 ms frames
                callback=callback,
                device=device,
            ):
                while not stop_event.is_set():
                    stop_event.wait(timeout=0.5)
        except Exception as exc:
            # An exception inside the daemon would otherwise terminate it
            # silently while the rest of the pipeline keeps polling an empty
            # queue forever. Surface it via stop_event so main.py can detect
            # the failure and shut down cleanly.
            log.error("Audio capture aborted: %s", exc, exc_info=True)
            stop_event.set()
            return

        log.info("Audio capture stopped")

    return start_daemon_thread("AudioCapture", run)


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
