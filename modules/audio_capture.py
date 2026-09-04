import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import numpy as np
import sounddevice as sd

from config import cfg
from utils.audio import rms as _rms
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import runtime_events
from modules.pipeline_events import AudioChunk

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
        self._pending = np.zeros(0, dtype=np.float32)

    def is_speech(self, frame: np.ndarray) -> bool:
        """Consume a continuous stream and classify all complete 32 ms windows."""
        import torch
        combined = np.concatenate([self._pending, np.asarray(frame, dtype=np.float32)])
        complete_samples = (len(combined) // self._WINDOW) * self._WINDOW
        detected = False
        for start in range(0, complete_samples, self._WINDOW):
            tensor = torch.from_numpy(combined[start : start + self._WINDOW]).float()
            if self._model(tensor, self._sr).item() >= self._threshold:
                detected = True
        self._pending = combined[complete_samples:].copy()
        return detected

    def reset(self) -> None:
        """Reset LSTM hidden states between utterances."""
        self._pending = np.zeros(0, dtype=np.float32)
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
        self._near_miss_min_speech = int(
            max(0.0, _cfg_float("vad_near_miss_min_speech_sec", 0.3)) * sr
        )
        self._max_speech       = int(cfg.audio.vad_max_speech_sec * sr)
        hard_max_sec = getattr(cfg.audio, "vad_hard_max_speech_sec", cfg.audio.vad_max_speech_sec)
        self._hard_max_speech  = int(max(cfg.audio.vad_max_speech_sec, hard_max_sec) * sr)
        self._overlap_samples  = int(max(0.0, getattr(cfg.audio, "vad_overlap_sec", 0.0)) * sr)
        self._near_miss_overlap_samples = int(
            max(0.0, _cfg_float("vad_near_miss_overlap_sec", 1.5)) * sr
        )
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
            self._discard_insufficient_speech("hard_max")
        elif silence_hit:
            # M3/M4: not enough speech at the silence gate. Discard now instead
            # of accumulating until hard_max — waiting added up-to-hard_max
            # latency for short utterances and prepended seconds of leading
            # silence (extra STT cost) to the next chunk. Near-miss speech is
            # preserved as overlap so a clipped word onset can still be heard.
            self._discard_insufficient_speech("silence")

    def _discard_insufficient_speech(self, boundary: str) -> None:
        was_adaptive = self._adaptive_active()
        raw_total_samples = self._total_samples
        speech_samples = self._speech_samples
        silent_samples = self._silent_samples
        raw_chunk = np.concatenate(self._buf)
        rms_value = _rms(raw_chunk)
        peak_value = float(np.max(np.abs(raw_chunk))) if len(raw_chunk) else 0.0
        if self._near_miss_min_speech <= speech_samples < self._min_speech:
            cut_reason = f"discard_{boundary}_near_miss_overlap"
            metrics.increment(f"audio.cut.{cut_reason}")
            if boundary == "silence":
                # Speech sits before the silence tail at a silence-gate discard:
                # trim the tail, then keep up to near_miss_overlap of the rest
                # (whole chunk when shorter) so the clipped onset survives.
                source = raw_chunk
                if 0 < silent_samples < len(raw_chunk):
                    source = raw_chunk[: -silent_samples]
                samples = min(self._near_miss_overlap_samples, len(source))
                next_overlap = (
                    source[-samples:].copy() if samples > 0 else np.zeros(0, dtype=np.float32)
                )
            else:
                next_overlap = self._next_overlap(
                    raw_chunk,
                    cut_reason,
                    self._near_miss_overlap_samples,
                )
            self._reset()
            self._pending_overlap = next_overlap
            self._emit_vad_runtime_event(
                cut_reason=cut_reason,
                audio_seconds=raw_total_samples / cfg.audio.sample_rate,
                raw_audio_seconds=raw_total_samples / cfg.audio.sample_rate,
                overlap_seconds=len(next_overlap) / cfg.audio.sample_rate,
                adaptive_active=was_adaptive,
                speech_seconds=speech_samples / cfg.audio.sample_rate,
                silence_seconds=silent_samples / cfg.audio.sample_rate,
                rms_value=rms_value,
                peak_value=peak_value,
                queue_drained=0,
            )
            return
        cut_reason = f"discard_{boundary}_no_speech"
        metrics.increment(f"audio.cut.{cut_reason}")
        # Pure-silence resets happen every silence-gate interval while idle;
        # only surface a runtime event when actual speech was thrown away.
        if speech_samples > 0:
            self._emit_vad_runtime_event(
                cut_reason=cut_reason,
                audio_seconds=raw_total_samples / cfg.audio.sample_rate,
                raw_audio_seconds=raw_total_samples / cfg.audio.sample_rate,
                overlap_seconds=0.0,
                adaptive_active=was_adaptive,
                speech_seconds=speech_samples / cfg.audio.sample_rate,
                silence_seconds=silent_samples / cfg.audio.sample_rate,
                rms_value=rms_value,
                peak_value=peak_value,
                queue_drained=0,
            )
        # Mostly silence — discard
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
        rms_value = _rms(chunk)
        peak_value = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
        overlap_sample_count = max(0, len(chunk) - raw_total_samples)
        overlap_seconds = overlap_sample_count / cfg.audio.sample_rate
        drained = put_latest(
            self._q,
            AudioChunk(
                audio=chunk,
                overlap_seconds=overlap_seconds,
                vad_cut_reason=cut_reason,
                raw_audio_seconds=raw_total_samples / cfg.audio.sample_rate,
            ),
            log,
            "audio_queue",
            "chunks",
        )
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

    def reset_stream(self) -> None:
        """Drop buffered audio and model state after a capture discontinuity."""
        self._adaptive_segments_remaining = 0
        self._reset(clear_overlap=True)


# ---------------------------------------------------------------------------
# Public start()
# ---------------------------------------------------------------------------

_FRAME_QUEUE_MAXSIZE = 200  # 32 ms VAD frames ≈ 6.4 s of headroom
_STREAM_READY_TIMEOUT_SEC = 5.0
_DEVICE_REJECTION_LIMIT = 8
_AUTO_DEVICE_KEYWORDS = ("cable output", "loopback", "stereo mix", "立體聲混音")


@dataclass(frozen=True)
class _CaptureDevice:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    default_samplerate: float


@dataclass(frozen=True)
class _CapturedFrame:
    audio: np.ndarray
    discontinuity_reason: str = ""


def start(audio_queue: queue.Queue, stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    # Resolve the loopback device synchronously so a missing device fails fast
    # in the caller's thread instead of silently killing a daemon thread.
    device = _find_loopback_device()
    startup_condition = threading.Condition()
    startup_state: dict[str, object] = {"status": "pending", "error": None}

    def publish_startup(status: str, error: Exception | None = None) -> bool:
        with startup_condition:
            if startup_state["status"] != "pending":
                return False
            startup_state["status"] = status
            startup_state["error"] = error
            startup_condition.notify_all()
            return True

    def run():
        worker: threading.Thread | None = None
        processor_ready = threading.Event()
        stream_open_started = time.monotonic()
        try:
            vad: _VadState | None = None
            chunk_samples = cfg.audio.sample_rate * cfg.audio.chunk_seconds

            # The sounddevice callback must return within one capture block or
            # PortAudio can overflow and silently drop audio. All heavy work (Silero
            # inference, buffer concatenation) therefore runs on a dedicated
            # worker thread; the callback only downmixes and enqueues the frame.
            frame_queue: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_MAXSIZE)
            frame_queue_lock = threading.Lock()
            frame_available = threading.Event()

            def callback(indata, frames, time_info, status):
                discontinuity_reason = ""
                if status:
                    log.warning("sounddevice status: %s", status)
                    if getattr(status, "input_overflow", False):
                        discontinuity_reason = "input_overflow"
                        metrics.increment("audio.input_overflow")
                if not processor_ready.is_set():
                    return
                if pause_event and pause_event.is_set():
                    return
                captured_frame = _CapturedFrame(
                    _downmix_to_mono(indata).copy(),
                    discontinuity_reason,
                )
                overloaded = False
                with frame_queue_lock:
                    try:
                        # copy(): sounddevice reuses the indata buffer after return.
                        frame_queue.put_nowait(captured_frame)
                    except queue.Full:
                        # A missing oldest frame puts the gap before every
                        # retained queued frame. Drop the stale backlog
                        # atomically and keep only the newest frame, tagged so
                        # reset occurs before it is processed.
                        overloaded = True
                        while True:
                            try:
                                frame_queue.get_nowait()
                            except queue.Empty:
                                break
                        frame_queue.put_nowait(
                            _CapturedFrame(
                                captured_frame.audio,
                                discontinuity_reason or "overload",
                            )
                        )
                    frame_available.set()
                if overloaded:
                    metrics.increment("audio.frames_dropped")

            def process_frames():
                fixed_buf = np.zeros(0, dtype=np.float32)
                was_paused = False

                def reset_processing_state(reason: str, *, drain: bool = True) -> None:
                    nonlocal fixed_buf
                    fixed_buf = np.zeros(0, dtype=np.float32)
                    if drain:
                        with frame_queue_lock:
                            while True:
                                try:
                                    frame_queue.get_nowait()
                                except queue.Empty:
                                    break
                            frame_available.clear()
                    if vad:
                        vad.reset_stream()
                    metrics.increment(f"audio.stream_reset.{reason}")

                while not stop_event.is_set():
                    if pause_event and pause_event.is_set():
                        if not was_paused:
                            reset_processing_state("pause")
                            was_paused = True
                        stop_event.wait(0.05)
                        continue
                    if was_paused:
                        # Close the callback/reset race at the resume edge.
                        reset_processing_state("resume")
                        was_paused = False
                    if not frame_available.wait(timeout=0.2):
                        continue
                    with frame_queue_lock:
                        try:
                            captured_frame = frame_queue.get_nowait()
                        except queue.Empty:
                            frame_available.clear()
                            continue
                        if frame_queue.empty():
                            frame_available.clear()
                    if captured_frame.discontinuity_reason:
                        reset_processing_state(
                            captured_frame.discontinuity_reason,
                            drain=False,
                        )
                    mono = captured_frame.audio
                    if vad:
                        vad.push(mono)
                        continue
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
                        put_latest(
                            audio_queue,
                            AudioChunk(
                                audio=chunk.copy(),
                                overlap_seconds=0.0,
                                vad_cut_reason="fixed",
                                raw_audio_seconds=len(chunk) / cfg.audio.sample_rate,
                            ),
                            log,
                            "audio_queue",
                            "chunks",
                        )

            def guarded_process_frames():
                try:
                    process_frames()
                except Exception as exc:
                    # The frame worker is a separate daemon thread, so its
                    # exception cannot reach this enclosing try/except.
                    log.error("Audio frame worker aborted: %s", exc, exc_info=True)
                    stop_event.set()

            mode = "VAD" if cfg.audio.vad_enabled else f"fixed {cfg.audio.chunk_seconds}s"
            capture_channels = getattr(cfg.audio, "capture_channels", cfg.audio.channels)
            capture_blocksize = (
                _SileroDetector._WINDOW
                if cfg.audio.vad_enabled
                else cfg.audio.sample_rate // 10
            )
            log.info(
                "Opening audio capture — device=[%d] %s host_api=%s mode=%s "
                "requested_samplerate=%d capture_channels=%d blocksize=%d",
                device.index,
                device.name,
                device.host_api,
                mode,
                cfg.audio.sample_rate,
                capture_channels,
                capture_blocksize,
            )

            with sd.InputStream(
                samplerate=cfg.audio.sample_rate,
                channels=capture_channels,
                dtype="float32",
                blocksize=capture_blocksize,
                callback=callback,
                device=device.index,
            ):
                stream_ready_ms = round(
                    (time.monotonic() - stream_open_started) * 1000,
                    2,
                )
                if stop_event.is_set() or not publish_startup("ready"):
                    return
                log.info(
                    "Audio stream ready — device=[%d] %s host_api=%s ready_ms=%.2f",
                    device.index,
                    device.name,
                    device.host_api,
                    stream_ready_ms,
                )
                runtime_events.emit(
                    "audio_startup",
                    status="ready",
                    device_index=device.index,
                    device_name=device.name[:160],
                    host_api=device.host_api[:80],
                    max_input_channels=device.max_input_channels,
                    default_samplerate=device.default_samplerate,
                    requested_samplerate=cfg.audio.sample_rate,
                    capture_channels=capture_channels,
                    dtype="float32",
                    blocksize=capture_blocksize,
                    mode=mode,
                    preflight_passed=True,
                    stream_ready_ms=stream_ready_ms,
                )

                # Stream availability is now proven. Load the optional model
                # behind a callback gate so first-run Torch Hub work cannot be
                # mistaken for an audio-open timeout or build a stale backlog.
                silero = None
                if cfg.audio.vad_enabled:
                    silero = _load_silero(cfg.audio.vad_silero_threshold)
                if stop_event.is_set():
                    return
                vad = _VadState(audio_queue, silero) if cfg.audio.vad_enabled else None
                worker = start_daemon_thread("AudioVadWorker", guarded_process_frames)
                processor_ready.set()
                while not stop_event.is_set():
                    stop_event.wait(timeout=0.5)
        except Exception as exc:
            # An exception inside the daemon would otherwise terminate it
            # silently while the rest of the pipeline keeps polling an empty
            # queue forever. Surface it via stop_event so main.py can detect
            # the failure and shut down cleanly.
            log.error("Audio capture aborted: %s", exc, exc_info=True)
            publish_startup("error", exc)
            stop_event.set()
            return
        finally:
            processor_ready.clear()
            if worker is not None:
                worker.join(timeout=2.0)

        log.info("Audio capture stopped")

    thread = start_daemon_thread("AudioCapture", run)
    with startup_condition:
        ready = startup_condition.wait_for(
            lambda: startup_state["status"] != "pending",
            timeout=_STREAM_READY_TIMEOUT_SEC,
        )
        if not ready:
            startup_state["status"] = "timeout"
    if not ready:
        stop_event.set()
        thread.join(timeout=1.0)
        raise RuntimeError(
            "Audio stream did not become ready within "
            f"{_STREAM_READY_TIMEOUT_SEC:.1f}s for device "
            f"[{device.index}] {device.name} ({device.host_api})"
        )
    if startup_state["status"] == "error":
        error = startup_state["error"]
        thread.join(timeout=1.0)
        raise RuntimeError(
            "Audio stream failed to open for device "
            f"[{device.index}] {device.name} ({device.host_api}): {error}"
        ) from error
    return thread


def _device_host_api_name(device: dict, host_apis: object) -> str:
    try:
        host_api_index = int(device.get("hostapi", -1))
        if host_api_index < 0:
            return "unknown"
        host_api = host_apis[host_api_index]
        return str(host_api.get("name") or "unknown")
    except (IndexError, KeyError, TypeError, ValueError):
        return "unknown"


def _capture_device(index: int, device: dict, host_apis: object) -> _CaptureDevice:
    try:
        max_input_channels = int(device.get("max_input_channels", 0) or 0)
    except (TypeError, ValueError):
        max_input_channels = 0
    try:
        default_samplerate = float(device.get("default_samplerate", 0.0) or 0.0)
    except (TypeError, ValueError):
        default_samplerate = 0.0
    return _CaptureDevice(
        index=index,
        name=str(device.get("name") or "unknown"),
        host_api=_device_host_api_name(device, host_apis),
        max_input_channels=max_input_channels,
        default_samplerate=default_samplerate,
    )


def _preflight_candidates(
    candidates: list[_CaptureDevice],
    *,
    sample_rate: int,
    capture_channels: int,
) -> tuple[list[_CaptureDevice], list[str]]:
    passing: list[_CaptureDevice] = []
    rejected: list[str] = []
    for candidate in candidates:
        if candidate.max_input_channels < capture_channels:
            rejected.append(
                f"[{candidate.index}] {candidate.name} ({candidate.host_api}): "
                f"needs {capture_channels} input channels, has {candidate.max_input_channels}"
            )
            continue
        try:
            sd.check_input_settings(
                device=candidate.index,
                channels=capture_channels,
                dtype="float32",
                samplerate=sample_rate,
            )
        except Exception as exc:
            rejected.append(
                f"[{candidate.index}] {candidate.name} ({candidate.host_api}): {exc}"
            )
            continue
        passing.append(candidate)
    return passing, rejected


def _bounded_rejections(rejected: list[str]) -> str:
    visible = rejected[:_DEVICE_REJECTION_LIMIT]
    if len(rejected) > len(visible):
        visible.append(f"... {len(rejected) - len(visible)} more")
    return "; ".join(visible) or "no matching input endpoints"


def _find_loopback_device() -> _CaptureDevice:
    devices = sd.query_devices()
    try:
        host_apis = sd.query_hostapis()
    except Exception:
        host_apis = []
    capture_channels = int(
        getattr(cfg.audio, "capture_channels", cfg.audio.channels)
    )
    sample_rate = int(cfg.audio.sample_rate)
    available = [
        _capture_device(index, device, host_apis)
        for index, device in enumerate(devices)
    ]

    configured_name = str(getattr(cfg.audio, "device_name", "") or "").strip()
    if configured_name:
        name_lower = configured_name.casefold()
        configured = [
            candidate
            for candidate in available
            if name_lower in candidate.name.casefold()
        ]
        if configured:
            passing, rejected = _preflight_candidates(
                configured,
                sample_rate=sample_rate,
                capture_channels=capture_channels,
            )
            if passing:
                selected = passing[0]
                log.info(
                    "Using configured device [%d]: %s (host_api=%s, preflight=passed)",
                    selected.index,
                    selected.name,
                    selected.host_api,
                )
                for reason in rejected:
                    log.debug("Rejected configured audio candidate: %s", reason)
                return selected
            raise RuntimeError(
                f"Configured audio device {configured_name!r} was found, but no "
                f"matching endpoint supports {sample_rate} Hz/{capture_channels}ch "
                f"float32. {_bounded_rejections(rejected)}"
            )
        log.warning("Configured device_name %r not found, falling back to auto-detect",
                    configured_name)

    automatic = [
        candidate
        for candidate in available
        if any(keyword in candidate.name.casefold() for keyword in _AUTO_DEVICE_KEYWORDS)
    ]
    passing, rejected = _preflight_candidates(
        automatic,
        sample_rate=sample_rate,
        capture_channels=capture_channels,
    )
    if passing:
        selected = passing[0]
        log.info(
            "Auto-detected audio device [%d]: %s (host_api=%s, preflight=passed)",
            selected.index,
            selected.name,
            selected.host_api,
        )
        for reason in rejected:
            log.debug("Rejected auto-detected audio candidate: %s", reason)
        return selected

    raise RuntimeError(
        f"No compatible loopback audio device found for {sample_rate} Hz/"
        f"{capture_channels}ch float32. {_bounded_rejections(rejected)}. "
        "Install VB-Cable or enable 'Stereo Mix', then restart. "
        "Run 'python modules/audio_capture.py' to list available devices."
    )


def _print_device_diagnostics() -> None:
    devices = sd.query_devices()
    try:
        host_apis = sd.query_hostapis()
    except Exception:
        host_apis = []
    capture_channels = int(
        getattr(cfg.audio, "capture_channels", cfg.audio.channels)
    )
    sample_rate = int(cfg.audio.sample_rate)
    print(
        "Available input devices "
        f"(requested format: {sample_rate} Hz/{capture_channels}ch float32):"
    )
    for index, raw_device in enumerate(devices):
        candidate = _capture_device(index, raw_device, host_apis)
        if candidate.max_input_channels <= 0:
            continue
        passing, rejected = _preflight_candidates(
            [candidate],
            sample_rate=sample_rate,
            capture_channels=capture_channels,
        )
        outcome = "PASS" if passing else f"FAIL: {_bounded_rejections(rejected)}"
        print(
            f"[{index}] {candidate.name} | host_api={candidate.host_api} | "
            f"max_input_channels={candidate.max_input_channels} | "
            f"default_samplerate={candidate.default_samplerate:g} | {outcome}"
        )
    selected = _find_loopback_device()
    print(
        f"Selected: [{selected.index}] {selected.name} "
        f"(host_api={selected.host_api}, preflight=passed)"
    )


if __name__ == "__main__":
    _print_device_diagnostics()
