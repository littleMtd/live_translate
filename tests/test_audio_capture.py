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
class TestSileroDetector(unittest.TestCase):
    def test_consumes_all_complete_windows_without_early_return(self):
        import types
        import modules.audio_capture as ac

        calls = []

        class _Tensor:
            def __init__(self, value):
                self.value = value
            def float(self):
                return self

        fake_torch = types.SimpleNamespace(from_numpy=lambda value: _Tensor(value.copy()))

        class _Model:
            def __call__(self, tensor, _sample_rate):
                calls.append(tensor.value)
                probability = 0.9 if len(calls) == 1 else 0.1
                return types.SimpleNamespace(item=lambda: probability)
            def reset_states(self):
                pass

        detector = ac._SileroDetector.__new__(ac._SileroDetector)
        detector._threshold = 0.5
        detector._sr = 16000
        detector._model = _Model()
        detector._pending = np.zeros(0, dtype=np.float32)

        with patch.dict(sys.modules, {"torch": fake_torch}):
            self.assertTrue(detector.is_speech(np.arange(1600, dtype=np.float32)))
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(detector._pending), 64)
            detector.is_speech(np.arange(448, dtype=np.float32))

        self.assertEqual(len(calls), 4)
        self.assertEqual(len(detector._pending), 0)


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
        return [
            {
                "name": name,
                "hostapi": 0,
                "max_input_channels": 2,
                "default_samplerate": 48000,
            }
            for name in names
        ]

    def _audio_cfg(self, device_name: str = ""):
        mock_cfg = MagicMock()
        mock_cfg.audio.device_name = device_name
        mock_cfg.audio.sample_rate = 16000
        mock_cfg.audio.capture_channels = 2
        mock_cfg.audio.channels = 1
        return mock_cfg

    def test_finds_stereo_mix(self):
        import modules.audio_capture as ac
        with patch.object(ac, "cfg", self._audio_cfg()), \
                patch.object(ac.sd, "query_devices", return_value=self._mock_devices(["Microphone", "Stereo Mix"])), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(ac.sd, "check_input_settings"):
            self.assertEqual(ac._find_loopback_device().index, 1)

    def test_finds_loopback_keyword(self):
        import modules.audio_capture as ac
        with patch.object(ac, "cfg", self._audio_cfg()), \
                patch.object(ac.sd, "query_devices", return_value=self._mock_devices(["Microphone", "WASAPI Loopback"])), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(ac.sd, "check_input_settings"):
            self.assertEqual(ac._find_loopback_device().index, 1)

    def test_raises_when_not_found(self):
        import modules.audio_capture as ac
        with patch.object(ac, "cfg", self._audio_cfg()), \
                patch.object(ac.sd, "query_devices", return_value=self._mock_devices(["Microphone", "Speaker"])), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(ac.sd, "check_input_settings"):
            with self.assertRaises(RuntimeError):
                ac._find_loopback_device()

    def test_device_name_config_takes_priority(self):
        import modules.audio_capture as ac
        with patch.object(ac, "cfg", self._audio_cfg("custom_device")), \
                patch.object(ac.sd, "query_devices", return_value=self._mock_devices(["Stereo Mix", "MY_CUSTOM_DEVICE"])), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(ac.sd, "check_input_settings"):
            self.assertEqual(ac._find_loopback_device().index, 1)

    def test_configured_candidates_preserve_passing_enumeration_order(self):
        import modules.audio_capture as ac
        devices = [
            {"name": "CABLE Output", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
            {"name": "CABLE Output", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 48000},
        ]
        with patch.object(ac, "cfg", self._audio_cfg("CABLE Output")), \
                patch.object(ac.sd, "query_devices", return_value=devices), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}, {"name": "WASAPI"}]), \
                patch.object(ac.sd, "check_input_settings") as check:
            selected = ac._find_loopback_device()

        self.assertEqual([call.kwargs["device"] for call in check.call_args_list], [0, 1])
        self.assertEqual(selected.index, 0)
        self.assertEqual(selected.host_api, "MME")

    def test_configured_matches_fail_closed_when_all_incompatible(self):
        import modules.audio_capture as ac
        devices = [
            {"name": "CABLE Output", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 44100},
            {"name": "CABLE Output", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 48000},
            {"name": "Stereo Mix", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
        ]

        def reject_wasapi(**kwargs):
            if kwargs["device"] == 1:
                raise RuntimeError("Invalid sample rate")

        with patch.object(ac, "cfg", self._audio_cfg("CABLE Output")), \
                patch.object(ac.sd, "query_devices", return_value=devices), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}, {"name": "WASAPI"}]), \
                patch.object(ac.sd, "check_input_settings", side_effect=reject_wasapi) as check:
            with self.assertRaisesRegex(RuntimeError, "no matching endpoint supports"):
                ac._find_loopback_device()

        self.assertEqual([call.kwargs["device"] for call in check.call_args_list], [1])

    def test_auto_detect_skips_incompatible_candidate(self):
        import modules.audio_capture as ac
        devices = self._mock_devices(["Stereo Mix", "CABLE Output"])

        def first_fails(**kwargs):
            if kwargs["device"] == 0:
                raise RuntimeError("Invalid sample rate")

        with patch.object(ac, "cfg", self._audio_cfg()), \
                patch.object(ac.sd, "query_devices", return_value=devices), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(ac.sd, "check_input_settings", side_effect=first_fails):
            selected = ac._find_loopback_device()

        self.assertEqual(selected.index, 1)

    def test_diagnostics_are_read_only(self):
        import modules.audio_capture as ac
        devices = self._mock_devices(["CABLE Output"])
        with patch.object(ac, "cfg", self._audio_cfg("CABLE Output")), \
                patch.object(ac.sd, "query_devices", return_value=devices), \
                patch.object(ac.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(ac.sd, "check_input_settings"), \
                patch.object(ac.sd, "InputStream") as input_stream, \
                patch.object(ac, "_load_silero") as load_silero, \
                patch.object(ac.runtime_events, "emit") as emit:
            ac._print_device_diagnostics()

        input_stream.assert_not_called()
        load_silero.assert_not_called()
        emit.assert_not_called()


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestStreamReadiness(unittest.TestCase):
    def _cfg(self):
        mock_cfg = MagicMock()
        mock_cfg.audio.sample_rate = 16000
        mock_cfg.audio.chunk_seconds = 1
        mock_cfg.audio.volume_threshold = 0.01
        mock_cfg.audio.vad_enabled = True
        mock_cfg.audio.vad_silero_threshold = 0.5
        mock_cfg.audio.capture_channels = 1
        mock_cfg.audio.channels = 1
        mock_cfg.audio.device_name = "CABLE Output"
        return mock_cfg

    def test_actual_stream_open_error_is_synchronous_and_skips_silero(self):
        import threading
        import modules.audio_capture as ac

        class _FailingStream:
            def __init__(self, **_kwargs):
                pass
            def __enter__(self):
                raise RuntimeError("device busy")
            def __exit__(self, *_exc):
                return False

        stop = threading.Event()
        with patch.object(ac, "cfg", self._cfg()), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(3, "CABLE Output", "MME", 2, 44100)), \
                patch.object(ac.sd, "InputStream", _FailingStream), \
                patch.object(ac, "_load_silero") as load_silero:
            with self.assertRaisesRegex(RuntimeError, "device busy"):
                ac.start(queue.Queue(), stop)

        self.assertTrue(stop.is_set())
        load_silero.assert_not_called()

    def test_stream_open_timeout_rejects_late_success_without_silero(self):
        import threading
        import time
        import modules.audio_capture as ac

        release = threading.Event()

        class _SlowStream:
            def __init__(self, **_kwargs):
                pass
            def __enter__(self):
                release.wait(timeout=2.0)
                return self
            def __exit__(self, *_exc):
                return False

        stop = threading.Event()
        started = time.monotonic()
        with patch.object(ac, "cfg", self._cfg()), \
                patch.object(ac, "_STREAM_READY_TIMEOUT_SEC", 0.05), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(3, "CABLE Output", "MME", 2, 44100)), \
                patch.object(ac.sd, "InputStream", _SlowStream), \
                patch.object(ac, "_load_silero") as load_silero:
            try:
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    ac.start(queue.Queue(), stop)
            finally:
                release.set()

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(stop.is_set())
        load_silero.assert_not_called()

    def test_start_returns_at_stream_ready_and_gates_silero_backlog(self):
        import threading
        import time
        import modules.audio_capture as ac

        captured = {}
        release_silero = threading.Event()
        silero_started = threading.Event()
        vad_initialized = threading.Event()
        processed = []

        class _FakeStream:
            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False

        def blocked_silero(_threshold):
            silero_started.set()
            release_silero.wait(timeout=2.0)
            return None

        class _FakeVad:
            def __init__(self, *_args):
                vad_initialized.set()
            def push(self, frame):
                processed.append(int(frame[0]))
            def reset_stream(self):
                pass

        stop = threading.Event()
        with patch.object(ac, "cfg", self._cfg()), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(3, "CABLE Output", "MME", 2, 44100)), \
                patch.object(ac.sd, "InputStream", _FakeStream), \
                patch.object(ac, "_load_silero", side_effect=blocked_silero), \
                patch.object(ac, "_VadState", _FakeVad):
            thread = ac.start(queue.Queue(), stop)
            self.assertTrue(silero_started.wait(timeout=1.0))
            captured["callback"](np.ones((512, 1), dtype=np.float32), 512, None, None)
            self.assertEqual(processed, [])

            release_silero.set()
            self.assertTrue(vad_initialized.wait(timeout=1.0))
            time.sleep(0.02)
            captured["callback"](np.full((512, 1), 7, dtype=np.float32), 512, None, None)
            deadline = time.monotonic() + 1.0
            while not processed and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            thread.join(timeout=2.0)

        self.assertEqual(processed, [7])


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
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(0, "Fake", "Test", 1, 100)), \
                patch.object(ac.sd, "InputStream", _FakeStream):
            thread = ac.start(audio_q, stop)
            time.sleep(0.05)
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

    def test_worker_exception_stops_pipeline(self):
        import threading
        import time
        import modules.audio_capture as ac

        cfg_mock = MagicMock()
        cfg_mock.audio.sample_rate = 100
        cfg_mock.audio.chunk_seconds = 1
        cfg_mock.audio.volume_threshold = 0.01
        cfg_mock.audio.vad_enabled = True
        cfg_mock.audio.capture_channels = 1
        cfg_mock.audio.channels = 1
        cfg_mock.audio.device_name = ""

        captured = {}
        vad_initialized = threading.Event()

        class _FakeStream:
            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        class _ExplodingVad:
            def __init__(self, *_args):
                vad_initialized.set()
            def push(self, _frame):
                raise RuntimeError("VAD inference failed")

        stop = threading.Event()
        with patch.object(ac, "cfg", cfg_mock), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(0, "Fake", "Test", 1, 100)), \
                patch.object(ac.sd, "InputStream", _FakeStream), \
                patch.object(ac, "_load_silero", return_value=None), \
                patch.object(ac, "_VadState", _ExplodingVad):
            thread = ac.start(queue.Queue(), stop)
            self.assertTrue(vad_initialized.wait(timeout=2.0))
            time.sleep(0.02)
            deadline = time.monotonic() + 2.0
            while "callback" not in captured and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("callback", captured)
            captured["callback"](np.ones((10, 1), dtype=np.float32), 10, None, None)
            deadline = time.monotonic() + 2.0
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            thread.join(timeout=2.0)

        self.assertTrue(stop.is_set(), "worker failure must stop the pipeline")

    def test_overload_keeps_latest_frames_and_resets_vad_stream(self):
        import threading
        import time
        import modules.audio_capture as ac

        cfg_mock = MagicMock()
        cfg_mock.audio.sample_rate = 100
        cfg_mock.audio.chunk_seconds = 1
        cfg_mock.audio.volume_threshold = 0.01
        cfg_mock.audio.vad_enabled = True
        cfg_mock.audio.vad_silero_threshold = 0.5
        cfg_mock.audio.capture_channels = 1
        cfg_mock.audio.channels = 1
        cfg_mock.audio.device_name = ""

        captured = {}
        release = threading.Event()
        vad_initialized = threading.Event()
        processed = []
        resets = []
        actions = []

        class _FakeStream:
            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        class _BlockingVad:
            def __init__(self, *_args):
                vad_initialized.set()
            def push(self, frame):
                value = int(frame[0])
                processed.append(value)
                actions.append(("push", value))
                if len(processed) == 1:
                    release.wait(timeout=2.0)
            def reset_stream(self):
                resets.append(True)
                actions.append(("reset", "overload"))

        stop = threading.Event()
        with patch.object(ac, "cfg", cfg_mock), \
                patch.object(ac, "_FRAME_QUEUE_MAXSIZE", 2), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(0, "Fake", "Test", 1, 100)), \
                patch.object(ac.sd, "InputStream", _FakeStream), \
                patch.object(ac, "_load_silero", return_value=None), \
                patch.object(ac, "_VadState", _BlockingVad):
            thread = ac.start(queue.Queue(), stop)
            self.assertTrue(vad_initialized.wait(timeout=2.0))
            time.sleep(0.02)
            deadline = time.monotonic() + 2.0
            while "callback" not in captured and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("callback", captured)

            for value in range(1, 6):
                frame = np.full((10, 1), value, dtype=np.float32)
                captured["callback"](frame, 10, None, None)
                if value == 1:
                    deadline = time.monotonic() + 1.0
                    while not processed and time.monotonic() < deadline:
                        time.sleep(0.01)
            release.set()
            deadline = time.monotonic() + 2.0
            while len(processed) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            thread.join(timeout=2.0)

        self.assertEqual(processed[0], 1)
        self.assertEqual(processed[-2:], [4, 5])
        self.assertTrue(resets, "VAD state must reset across dropped audio")
        self.assertEqual(
            actions,
            [
                ("push", 1),
                ("reset", "overload"),
                ("push", 4),
                ("push", 5),
            ],
        )

    def test_input_overflow_resets_once_before_next_frame(self):
        import threading
        import time
        import modules.audio_capture as ac

        cfg_mock = MagicMock()
        cfg_mock.audio.sample_rate = 16000
        cfg_mock.audio.chunk_seconds = 1
        cfg_mock.audio.volume_threshold = 0.01
        cfg_mock.audio.vad_enabled = True
        cfg_mock.audio.vad_silero_threshold = 0.5
        cfg_mock.audio.capture_channels = 1
        cfg_mock.audio.channels = 1
        cfg_mock.audio.device_name = ""

        captured = {}
        vad_initialized = threading.Event()
        processed = []
        resets = []

        class _FakeStream:
            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False

        class _FakeVad:
            def __init__(self, *_args):
                vad_initialized.set()
            def push(self, frame):
                processed.append(int(frame[0]))
            def reset_stream(self):
                resets.append(True)

        class _Status:
            def __init__(self, input_overflow):
                self.input_overflow = input_overflow
            def __bool__(self):
                return True
            def __str__(self):
                return "input overflow" if self.input_overflow else "other status"

        stop = threading.Event()
        with patch.object(ac, "cfg", cfg_mock), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(0, "Fake", "Test", 1, 16000)), \
                patch.object(ac.sd, "InputStream", _FakeStream), \
                patch.object(ac, "_load_silero", return_value=None), \
                patch.object(ac, "_VadState", _FakeVad), \
                patch.object(ac, "metrics") as metrics:
            thread = ac.start(queue.Queue(), stop)
            self.assertTrue(vad_initialized.wait(timeout=1.0))
            time.sleep(0.02)
            captured["callback"](
                np.full((512, 1), 1, dtype=np.float32),
                512,
                None,
                _Status(True),
            )
            deadline = time.monotonic() + 1.0
            while len(processed) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)

            captured["callback"](
                np.full((512, 1), 2, dtype=np.float32),
                512,
                None,
                _Status(False),
            )
            deadline = time.monotonic() + 1.0
            while len(processed) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            thread.join(timeout=2.0)

        self.assertEqual(processed, [1, 2])
        self.assertEqual(len(resets), 1)
        metrics.increment.assert_any_call("audio.input_overflow")
        metrics.increment.assert_any_call("audio.stream_reset.input_overflow")

    def test_input_overflow_reset_is_bound_to_post_gap_frame_with_backlog(self):
        import threading
        import time
        import modules.audio_capture as ac

        cfg_mock = MagicMock()
        cfg_mock.audio.sample_rate = 16000
        cfg_mock.audio.chunk_seconds = 1
        cfg_mock.audio.volume_threshold = 0.01
        cfg_mock.audio.vad_enabled = True
        cfg_mock.audio.vad_silero_threshold = 0.5
        cfg_mock.audio.capture_channels = 1
        cfg_mock.audio.channels = 1
        cfg_mock.audio.device_name = ""

        captured = {}
        vad_initialized = threading.Event()
        release_first = threading.Event()
        actions = []

        class _FakeStream:
            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                return False

        class _BlockingVad:
            def __init__(self, *_args):
                vad_initialized.set()
            def push(self, frame):
                value = int(frame[0])
                actions.append(("push", value))
                if value == 1:
                    release_first.wait(timeout=2.0)
            def reset_stream(self):
                actions.append(("reset", "input_overflow"))

        class _OverflowStatus:
            input_overflow = True
            def __bool__(self):
                return True
            def __str__(self):
                return "input overflow"

        stop = threading.Event()
        with patch.object(ac, "cfg", cfg_mock), \
                patch.object(ac, "_find_loopback_device", return_value=ac._CaptureDevice(0, "Fake", "Test", 1, 16000)), \
                patch.object(ac.sd, "InputStream", _FakeStream), \
                patch.object(ac, "_load_silero", return_value=None), \
                patch.object(ac, "_VadState", _BlockingVad):
            thread = ac.start(queue.Queue(), stop)
            self.assertTrue(vad_initialized.wait(timeout=1.0))
            time.sleep(0.02)
            for value, status in (
                (1, None),
                (2, None),
                (3, None),
                (4, _OverflowStatus()),
            ):
                captured["callback"](
                    np.full((512, 1), value, dtype=np.float32),
                    512,
                    None,
                    status,
                )
                if value == 1:
                    deadline = time.monotonic() + 1.0
                    while ("push", 1) not in actions and time.monotonic() < deadline:
                        time.sleep(0.01)
            release_first.set()
            deadline = time.monotonic() + 1.0
            while ("push", 4) not in actions and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            thread.join(timeout=2.0)

        self.assertEqual(
            actions,
            [
                ("push", 1),
                ("push", 2),
                ("push", 3),
                ("reset", "input_overflow"),
                ("push", 4),
            ],
        )


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
