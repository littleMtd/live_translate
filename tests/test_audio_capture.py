import sys

# Stub missing packages so the module under test can be imported without a venv
from unittest.mock import MagicMock, patch
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = MagicMock()

import queue
import unittest
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    # Stub numpy so audio_capture.py can be imported; RMS tests are skipped anyway
    if "numpy" not in sys.modules:
        sys.modules["numpy"] = MagicMock()


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestRms(unittest.TestCase):

    def test_silence_is_zero(self):
        from utils.audio import rms
        audio = np.zeros(1600, dtype=np.float32)
        self.assertAlmostEqual(rms(audio), 0.0)

    def test_empty_audio_is_zero(self):
        from utils.audio import rms
        audio = np.array([], dtype=np.float32)
        self.assertEqual(rms(audio), 0.0)

    def test_full_amplitude_sine(self):
        from utils.audio import rms
        t = np.linspace(0, 1, 16000)
        audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        # RMS of sine = 1/sqrt(2) ≈ 0.707
        self.assertAlmostEqual(rms(audio), 1.0 / np.sqrt(2), places=2)

    def test_rms_above_threshold_for_loud_audio(self):
        from utils.audio import rms
        from config import cfg
        loud = np.ones(1600, dtype=np.float32) * 0.5
        self.assertGreater(rms(loud), cfg.audio.volume_threshold)

    def test_rms_below_threshold_for_silence(self):
        from utils.audio import rms
        from config import cfg
        silent = np.zeros(1600, dtype=np.float32)
        self.assertLess(rms(silent), cfg.audio.volume_threshold)

    def test_audio_capture_keeps_legacy_rms_alias(self):
        from modules.audio_capture import _rms
        audio = np.ones(10, dtype=np.float32)
        self.assertAlmostEqual(_rms(audio), 1.0)

    def test_downmix_to_mono_averages_stereo_channels(self):
        from modules.audio_capture import _downmix_to_mono
        stereo = np.array(
            [
                [1.0, -1.0],
                [0.5, 0.25],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        mono = _downmix_to_mono(stereo)

        self.assertEqual(mono.dtype, np.float32)
        np.testing.assert_allclose(mono, np.array([0.0, 0.375, 0.5], dtype=np.float32))

    def test_downmix_to_mono_keeps_single_channel_audio(self):
        from modules.audio_capture import _downmix_to_mono
        one_channel = np.array([[0.25], [0.5]], dtype=np.float32)

        mono = _downmix_to_mono(one_channel)

        np.testing.assert_allclose(mono, np.array([0.25, 0.5], dtype=np.float32))

    def test_downmix_to_mono_flattens_1d_audio(self):
        from modules.audio_capture import _downmix_to_mono
        audio = np.array([0.1, 0.2], dtype=np.float32)

        mono = _downmix_to_mono(audio)

        np.testing.assert_allclose(mono, audio)


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestVadState(unittest.TestCase):
    """Tests for _VadState — the VAD chunking state machine."""

    # Use small sample-rate-equivalent values so tests run instantly
    _SR = 100          # fake "sample rate" for config patching
    _THRESHOLD = 0.01
    _SILENCE_SEC = 0.1   # silence_gate  = 10 samples
    _MIN_SEC = 0.05      # min_speech     =  5 samples
    _MAX_SEC = 0.5       # max_speech     = 50 samples

    def _make_cfg(self):
        m = MagicMock()
        m.audio.sample_rate = self._SR
        m.audio.volume_threshold = self._THRESHOLD
        m.audio.vad_silence_sec = self._SILENCE_SEC
        m.audio.vad_min_speech_sec = self._MIN_SEC
        m.audio.vad_near_miss_min_speech_sec = 0.03
        m.audio.vad_max_speech_sec = self._MAX_SEC
        m.audio.vad_hard_max_speech_sec = self._MAX_SEC
        m.audio.vad_overlap_sec = 0.0
        m.audio.vad_near_miss_overlap_sec = 0.2
        m.audio.vad_silence_overlap_sec = 0.0
        m.audio.vad_adaptive_enabled = False
        m.audio.vad_adaptive_after_boundary_cuts = 1
        m.audio.vad_adaptive_silence_sec = self._SILENCE_SEC
        m.audio.vad_adaptive_max_speech_sec = self._MAX_SEC
        m.audio.vad_adaptive_hard_max_speech_sec = self._MAX_SEC
        m.audio.vad_adaptive_overlap_sec = 0.0
        return m

    def _loud(self, n=10):
        return np.ones(n, dtype=np.float32) * 0.5   # rms = 0.5 >> threshold

    def _quiet(self, n=10):
        return np.zeros(n, dtype=np.float32)

    def test_emit_after_speech_then_silence(self):
        from modules.audio_capture import _VadState
        from modules.pipeline_events import AudioChunk
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(10))   # speech_samples=10 >= min 5
            vad.push(self._quiet(10))  # silent_samples=10 >= gate 10 → emit
        self.assertFalse(q.empty(), "Expected chunk to be emitted")
        chunk = q.get_nowait()
        self.assertIsInstance(chunk, AudioChunk)
        self.assertEqual(len(chunk), 20)
        self.assertEqual(chunk.vad_cut_reason, "silence")
        self.assertEqual(chunk.overlap_seconds, 0.0)

    def test_no_emit_silence_below_gate(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad.push(self._quiet(5))   # silent_samples=5 < gate 10 → no emit
        self.assertTrue(q.empty())

    def test_no_emit_speech_below_min(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(3))    # speech_samples=3 < min 5
            vad.push(self._quiet(10))  # silence gate hit, but not enough speech
        self.assertTrue(q.empty())

    def test_emit_at_max_speech_with_enough_speech(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            # Push 5 loud frames of 10 samples each → total=50 >= max 50
            for _ in range(5):
                vad.push(self._loud(10))
        self.assertFalse(q.empty(), "Expected force-emit at max_speech")

    def test_soft_max_waits_for_pause_before_hard_max(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        cfg = self._make_cfg()
        cfg.audio.vad_hard_max_speech_sec = 0.8
        with patch("modules.audio_capture.cfg", cfg):
            vad = _VadState(q)
            # Reach soft max with continuous speech. This should not cut yet.
            for _ in range(5):
                vad.push(self._loud(10))
            self.assertTrue(q.empty())
            # A short pause after soft max is enough to emit coherently.
            vad.push(self._quiet(5))
        self.assertFalse(q.empty(), "Expected soft-max chunk to emit on next pause")

    def test_soft_max_overlap_prefixes_next_emitted_chunk(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        cfg = self._make_cfg()
        cfg.audio.vad_hard_max_speech_sec = 0.8
        cfg.audio.vad_overlap_sec = 0.1  # 10 samples
        with patch("modules.audio_capture.cfg", cfg):
            vad = _VadState(q)
            for _ in range(5):
                vad.push(self._loud(10))
            vad.push(self._quiet(5))  # soft-max emit, keeps 10-sample overlap
            first = q.get_nowait()

            vad.push(self._loud(10))
            vad.push(self._quiet(10))  # natural emit, prefixed with previous overlap
            second = q.get_nowait()

        self.assertEqual(len(first), 55)
        self.assertEqual(len(second), 30)
        self.assertEqual(second.overlap_seconds, 0.1)
        self.assertEqual(second.vad_cut_reason, "silence")

    def test_natural_silence_cut_does_not_create_overlap(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        cfg = self._make_cfg()
        cfg.audio.vad_overlap_sec = 0.1
        with patch("modules.audio_capture.cfg", cfg):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad.push(self._quiet(10))
            first = q.get_nowait()

            vad.push(self._loud(10))
            vad.push(self._quiet(10))
            second = q.get_nowait()

        self.assertEqual(len(first), 20)
        self.assertEqual(len(second), 20)

    def test_natural_silence_cut_uses_short_speech_overlap(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        cfg = self._make_cfg()
        cfg.audio.vad_silence_overlap_sec = 0.1
        with patch("modules.audio_capture.cfg", cfg):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad.push(self._quiet(10))
            first = q.get_nowait()

            vad.push(self._loud(10))
            vad.push(self._quiet(10))
            second = q.get_nowait()

        self.assertEqual(len(first), 20)
        self.assertEqual(len(second), 30)
        np.testing.assert_allclose(second[:10], self._loud(10))

    def test_adaptive_vad_extends_next_segment_after_boundary_cut(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        cfg = self._make_cfg()
        cfg.audio.vad_hard_max_speech_sec = 0.8
        cfg.audio.vad_overlap_sec = 0.1
        cfg.audio.vad_adaptive_enabled = True
        cfg.audio.vad_adaptive_after_boundary_cuts = 1
        cfg.audio.vad_adaptive_silence_sec = 0.2
        cfg.audio.vad_adaptive_max_speech_sec = 0.7
        cfg.audio.vad_adaptive_hard_max_speech_sec = 0.9
        cfg.audio.vad_adaptive_overlap_sec = 0.2
        with patch("modules.audio_capture.cfg", cfg):
            vad = _VadState(q)
            for _ in range(5):
                vad.push(self._loud(10))
            vad.push(self._quiet(5))  # soft-max emit activates adaptive next segment
            first = q.get_nowait()
            self.assertEqual(len(first), 55)
            self.assertEqual(vad._adaptive_segments_remaining, 1)

            for _ in range(5):
                vad.push(self._loud(10))
            vad.push(self._quiet(10))  # base silence gate hit, adaptive gate not yet
            self.assertTrue(q.empty())
            vad.push(self._quiet(10))  # adaptive silence gate hit
            second = q.get_nowait()

        self.assertEqual(len(second), 80)

    def test_reset_at_max_speech_without_speech(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            # Push 5 quiet frames → max hit, but no speech → discard
            for _ in range(5):
                vad.push(self._quiet(10))
        self.assertTrue(q.empty(), "Silence-only max should be discarded, not emitted")

    def test_hard_max_near_miss_retains_tail_overlap(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        cfg = self._make_cfg()
        with patch("modules.audio_capture.cfg", cfg), \
                patch("modules.audio_capture.runtime_events.emit") as emit:
            vad = _VadState(q)
            vad.push(self._loud(3))    # near-miss speech: >= 3 samples, < min 5
            vad.push(self._quiet(47))  # hard max reached; keep tail overlap only

            self.assertTrue(q.empty())
            self.assertEqual(len(vad._pending_overlap), 20)
            emit.assert_called_once()
            self.assertEqual(emit.call_args.kwargs["cut_reason"], "discard_hard_max_near_miss_overlap")
            self.assertGreater(emit.call_args.kwargs["rms"], 0.0)
            self.assertEqual(emit.call_args.kwargs["peak"], 0.5)

            vad.push(self._loud(10))
            vad.push(self._quiet(10))
            chunk = q.get_nowait()

        self.assertEqual(len(chunk), 40)
        self.assertEqual(chunk.overlap_seconds, 0.2)
        self.assertEqual(chunk.vad_cut_reason, "silence")

    def test_reset_clears_state(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad._reset()
            self.assertEqual(vad._speech_samples, 0)
            self.assertEqual(vad._silent_samples, 0)
            self.assertEqual(vad._total_samples, 0)
            self.assertEqual(vad._buf, [])

    def test_emitted_chunk_is_float32(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad.push(self._quiet(10))
        chunk = q.get_nowait()
        self.assertEqual(chunk.dtype, np.float32)

    def test_queue_full_drops_oldest_and_enqueues_new(self):
        from modules.audio_capture import _VadState
        q = queue.Queue(maxsize=1)
        sentinel = np.zeros(1, dtype=np.float32)
        q.put_nowait(sentinel)   # fill the queue
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad.push(self._quiet(10))   # triggers emit → should drain + re-add
        self.assertFalse(q.empty())
        chunk = q.get_nowait()
        self.assertEqual(len(chunk), 20)  # the new chunk, not the sentinel

    def test_speech_samples_reset_after_emit(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(10))
            vad.push(self._quiet(10))   # emit
            # After emit, state should be clean
            self.assertEqual(vad._speech_samples, 0)
            self.assertEqual(vad._silent_samples, 0)


class TestStartFailsFast(unittest.TestCase):
    """`start()` must surface device errors synchronously so main.py can detect them."""

    def test_start_raises_when_device_missing(self):
        import modules.audio_capture as ac

        q: queue.Queue = queue.Queue()
        stop = __import__("threading").Event()

        with patch.object(ac.sd, "query_devices",
                          return_value=[{"name": "Microphone"}, {"name": "Speaker"}]):
            with self.assertRaises(RuntimeError):
                ac.start(q, stop)

        # No daemon thread should have been spawned; stop_event remains untouched.
        self.assertFalse(stop.is_set())


class TestFindLoopbackDevice(unittest.TestCase):

    def _mock_devices(self, names: list[str]) -> list[dict]:
        return [{"name": n} for n in names]

    def test_finds_stereo_mix(self):
        import modules.audio_capture as ac
        with patch.object(ac.sd, "query_devices",
                          return_value=self._mock_devices(["Microphone", "Stereo Mix"])):
            self.assertEqual(ac._find_loopback_device(), 1)

    def test_finds_loopback_keyword(self):
        import modules.audio_capture as ac
        with patch.object(ac.sd, "query_devices",
                          return_value=self._mock_devices(["Microphone", "WASAPI Loopback"])):
            self.assertEqual(ac._find_loopback_device(), 1)

    def test_raises_when_not_found(self):
        import modules.audio_capture as ac
        with patch.object(ac.sd, "query_devices",
                          return_value=self._mock_devices(["Microphone", "Speaker"])):
            with self.assertRaises(RuntimeError):
                ac._find_loopback_device()

    def test_device_name_config_takes_priority(self):
        import modules.audio_capture as ac
        mock_cfg = MagicMock()
        mock_cfg.audio.device_name = "custom_device"
        with patch.object(ac.sd, "query_devices",
                          return_value=self._mock_devices(["Stereo Mix", "MY_CUSTOM_DEVICE"])), \
             patch("modules.audio_capture.cfg", mock_cfg):
            self.assertEqual(ac._find_loopback_device(), 1)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestCallbackOffloadsToWorker(unittest.TestCase):
    """H3: the sounddevice callback must only enqueue frames; chunking happens
    on the worker thread and still reaches audio_queue."""

    def test_fixed_mode_chunks_emitted_via_worker(self):
        import threading
        import time
        import modules.audio_capture as ac

        cfg_mock = MagicMock()
        cfg_mock.audio.sample_rate = 100
        cfg_mock.audio.chunk_seconds = 1          # chunk = 100 samples
        cfg_mock.audio.volume_threshold = 0.01
        cfg_mock.audio.vad_enabled = False
        cfg_mock.audio.capture_channels = 1
        cfg_mock.audio.channels = 1
        cfg_mock.audio.device_name = ""

        captured = {}

        class _FakeStream:
            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        audio_q: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch.object(ac, "cfg", cfg_mock), \
                patch.object(ac, "_find_loopback_device", return_value=0), \
                patch.object(ac.sd, "InputStream", _FakeStream):
            thread = ac.start(audio_q, stop)
            deadline = time.monotonic() + 2.0
            while "callback" not in captured and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("callback", captured, "InputStream callback not registered")

            loud = np.ones((10, 1), dtype=np.float32) * 0.5
            for _ in range(12):   # 120 samples > one 100-sample chunk
                captured["callback"](loud, 10, None, None)

            chunk = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    chunk = audio_q.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
            stop.set()
            thread.join(timeout=2)

        self.assertIsNotNone(chunk, "worker thread must emit the fixed chunk")
        self.assertEqual(chunk.vad_cut_reason, "fixed")
        self.assertEqual(len(chunk.audio), 100)


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestVadEarlyDiscardAtSilenceGate(unittest.TestCase):
    """M3/M4: insufficient speech is discarded at the silence gate instead of
    accumulating (with leading silence) until hard_max."""

    def _make_cfg(self):
        m = MagicMock()
        m.audio.sample_rate = 100
        m.audio.volume_threshold = 0.01
        m.audio.vad_silence_sec = 0.1          # gate = 10 samples
        m.audio.vad_min_speech_sec = 0.05      # min  =  5 samples
        m.audio.vad_near_miss_min_speech_sec = 0.03
        m.audio.vad_max_speech_sec = 0.5
        m.audio.vad_hard_max_speech_sec = 0.5
        m.audio.vad_overlap_sec = 0.0
        m.audio.vad_near_miss_overlap_sec = 0.2
        m.audio.vad_silence_overlap_sec = 0.0
        m.audio.vad_adaptive_enabled = False
        m.audio.vad_adaptive_after_boundary_cuts = 1
        m.audio.vad_adaptive_silence_sec = 0.1
        m.audio.vad_adaptive_max_speech_sec = 0.5
        m.audio.vad_adaptive_hard_max_speech_sec = 0.5
        m.audio.vad_adaptive_overlap_sec = 0.0
        return m

    def _loud(self, n):
        return np.ones(n, dtype=np.float32) * 0.5

    def _quiet(self, n):
        return np.zeros(n, dtype=np.float32)

    def test_pure_silence_resets_buffer_at_gate(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._quiet(10))   # silence gate hit, zero speech
            self.assertEqual(vad._total_samples, 0, "buffer must reset at gate")
            self.assertEqual(len(vad._pending_overlap), 0)
        self.assertTrue(q.empty())

    def test_near_miss_speech_keeps_overlap_at_gate(self):
        from modules.audio_capture import _VadState
        q = queue.Queue()
        with patch("modules.audio_capture.cfg", self._make_cfg()):
            vad = _VadState(q)
            vad.push(self._loud(3))     # 3 = near_miss min, < min_speech 5
            vad.push(self._quiet(10))   # silence gate → near-miss discard
            self.assertEqual(vad._total_samples, 0, "buffer must reset at gate")
            self.assertGreater(len(vad._pending_overlap), 0,
                               "near-miss speech must be kept as overlap")
        self.assertTrue(q.empty())
