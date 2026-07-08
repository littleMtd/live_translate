import sys

# Stub heavy optional packages so the module can be imported without a GPU venv
from unittest.mock import MagicMock, patch
for _mod in ("funasr", "groq", "soundfile"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import queue
import re
import threading
import time
import unittest

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    if "numpy" not in sys.modules:
        sys.modules["numpy"] = MagicMock()

from modules.stt import (
    _CONSECUTIVE_NONE_WARN,
    _NOISE_TAGS,
    _SENSEVOICE_PROBE_EVERY,
    _TAG_RE,
    _dedupe_segments_by_timestamp,
    _normalize_audio_for_stt,
    STTEngine,
)
from modules.pipeline_events import AudioChunk, SegmentInfo, TranscriptionEvent


# ---------------------------------------------------------------------------
# Regex / constant sanity checks  (no numpy needed)
# ---------------------------------------------------------------------------

class TestTagRegex(unittest.TestCase):

    def test_strips_language_tag(self):
        self.assertEqual(_TAG_RE.sub("", "<|ko|>안녕"), "안녕")

    def test_strips_emotion_tag(self):
        self.assertEqual(_TAG_RE.sub("", "<|EMO_UNKNOWN|>text"), "text")

    def test_strips_speech_tag(self):
        self.assertEqual(_TAG_RE.sub("", "<|Speech|>text"), "text")

    def test_strips_multiple_tags(self):
        raw = "<|ko|><|EMO_UNKNOWN|><|Speech|><|withitn|>안녕하세요"
        self.assertEqual(_TAG_RE.sub("", raw).strip(), "안녕하세요")

    def test_does_not_strip_normal_angle_brackets(self):
        # Angle brackets without pipes should not be stripped
        result = _TAG_RE.sub("", "a<b>c")
        self.assertEqual(result, "a<b>c")

    def test_leaves_plain_text_unchanged(self):
        self.assertEqual(_TAG_RE.sub("", "안녕하세요"), "안녕하세요")


class TestNoiseTags(unittest.TestCase):

    def test_bgm_in_noise_tags(self):
        self.assertIn("<|BGM|>", _NOISE_TAGS)

    def test_laughter_in_noise_tags(self):
        self.assertIn("<|Laughter|>", _NOISE_TAGS)

    def test_applause_in_noise_tags(self):
        self.assertIn("<|Applause|>", _NOISE_TAGS)

    def test_speech_not_in_noise_tags(self):
        self.assertNotIn("<|Speech|>", _NOISE_TAGS)


# ---------------------------------------------------------------------------
# STTEngine._transcribe_sensevoice  (mocked model, numpy required)
# ---------------------------------------------------------------------------

def _make_engine_sv() -> STTEngine:
    """Build an STTEngine with a mocked SenseVoice model."""
    eng = STTEngine.__new__(STTEngine)
    eng._sense_voice = MagicMock()
    eng._groq_client = None
    eng._groq_fallback_client = None
    eng._use_groq = False
    eng._consecutive_none = 0
    eng._sv_fallback_counter = 0
    eng._groq_rate_limited_until = 0.0
    eng._groq_fallback_rate_limited_until = 0.0
    eng._groq_prefer_fallback_key = False
    eng._last_transcript = ""
    eng._last_context_transcript = ""
    eng._last_context_updated_at = None
    eng._last_context_gate_reason = ""
    eng._last_prompt_context_gated = False
    eng._last_prompt_context_gate_reason = ""
    eng._utterance_seq = 0
    eng._current_utterance_id = ""
    eng._last_audio_seconds = 0.0
    eng._last_segments = ()
    eng._last_timestamp_deduped_segments = 0
    eng._last_timestamp_deduped_chars = 0
    eng._current_overlap_seconds = 0.0
    eng._current_vad_cut_reason = ""
    eng._last_prompt_budget = None
    eng._last_sensevoice_error = False
    return eng


def _sv_response(text: str):
    return [{"text": text}]


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestTranscribeSenseVoice(unittest.TestCase):

    def _audio(self, n=1600):
        return np.zeros(n, dtype=np.float32)

    def test_returns_clean_text(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response(
            "<|ko|><|EMO_UNKNOWN|><|Speech|><|withitn|>안녕하세요"
        )
        result = eng._transcribe_sensevoice(self._audio())
        self.assertEqual(result, "안녕하세요")

    def test_pure_noise_returns_none(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response("<|BGM|>")
        self.assertIsNone(eng._transcribe_sensevoice(self._audio()))

    def test_empty_text_returns_none(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response("")
        self.assertIsNone(eng._transcribe_sensevoice(self._audio()))

    def test_only_metadata_tags_returns_none(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response(
            "<|ko|><|EMO_UNKNOWN|>"
        )
        self.assertIsNone(eng._transcribe_sensevoice(self._audio()))

    def test_exception_returns_none(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.side_effect = RuntimeError("CUDA error")
        self.assertIsNone(eng._transcribe_sensevoice(self._audio()))

    def test_empty_generate_result_returns_none(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = []
        self.assertIsNone(eng._transcribe_sensevoice(self._audio()))

    def test_text_with_noise_and_speech_tag_is_kept(self):
        # If both speech and noise tags are present, speech wins
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response(
            "<|Speech|><|BGM|>음악과 함께"
        )
        result = eng._transcribe_sensevoice(self._audio())
        self.assertIsNotNone(result)
        self.assertIn("음악과 함께", result)


# ---------------------------------------------------------------------------
# STTEngine._transcribe_groq  (mocked client, numpy required)
# ---------------------------------------------------------------------------

class TestTimestampDedupe(unittest.TestCase):
    def test_kept_empty_segment_text_does_not_restore_dropped_text(self):
        text, kept, dropped, deduped_chars = _dedupe_segments_by_timestamp(
            "old words new words",
            [
                {"start": 0.0, "end": 0.8, "text": "old words"},
                {"start": 0.9, "end": 1.4, "text": ""},
            ],
            1.2,
        )

        self.assertEqual(text, "")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(deduped_chars, len("old words"))


def _make_groq_resp(text: str, language: str = "ko", segments: list[dict] | None = None) -> MagicMock:
    """Build a mock verbose_json Groq response object."""
    resp = MagicMock()
    resp.text = text
    resp.language = language
    resp.segments = segments or []
    return resp


def _make_engine_groq(response_text: str = "안녕하세요") -> STTEngine:
    eng = STTEngine.__new__(STTEngine)
    eng._sense_voice = None
    eng._use_groq = True
    eng._groq_client = MagicMock()
    eng._groq_client.audio.transcriptions.create.return_value = _make_groq_resp(response_text)
    eng._groq_fallback_client = None
    eng._consecutive_none = 0
    eng._sv_fallback_counter = 0
    eng._groq_rate_limited_until = 0.0
    eng._groq_fallback_rate_limited_until = 0.0
    eng._last_transcript = ""
    eng._last_context_transcript = ""
    eng._last_context_updated_at = None
    eng._last_context_gate_reason = ""
    eng._last_prompt_context_gated = False
    eng._last_prompt_context_gate_reason = ""
    eng._utterance_seq = 0
    eng._current_utterance_id = ""
    eng._last_audio_seconds = 0.0
    eng._last_segments = ()
    eng._last_timestamp_deduped_segments = 0
    eng._last_timestamp_deduped_chars = 0
    eng._current_overlap_seconds = 0.0
    eng._current_vad_cut_reason = ""
    eng._last_prompt_budget = None
    eng._last_sensevoice_error = False
    return eng


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestTranscribeGroq(unittest.TestCase):

    def _audio(self, n=16000):
        return np.full(n, 0.1, dtype=np.float32)  # RMS=0.1 > volume_threshold

    def test_normalize_audio_for_stt_targets_rms_without_mutating_input(self):
        audio = np.full(1600, 0.02, dtype=np.float32)

        normalized, stats = _normalize_audio_for_stt(audio)

        self.assertIsNot(normalized, audio)
        self.assertAlmostEqual(float(np.sqrt(np.mean(np.square(audio)))), 0.02, places=3)
        self.assertLessEqual(stats["normalization_gain"], 4.0)
        self.assertAlmostEqual(stats["normalized_rms"], 0.08, places=2)

    def test_normalize_audio_for_stt_peak_limits(self):
        audio = np.array([0.01, 0.9, -0.9], dtype=np.float32)

        mock_cfg = MagicMock()
        mock_cfg.audio.stt_normalize_enabled = True
        mock_cfg.audio.stt_target_rms = 1.0
        mock_cfg.audio.stt_max_gain = 4.0
        mock_cfg.audio.stt_peak_limit = 0.5
        with patch("modules.stt.cfg", mock_cfg):
            normalized, stats = _normalize_audio_for_stt(audio)

        self.assertLessEqual(float(np.max(np.abs(normalized))), 0.5)
        self.assertTrue(stats["normalization_limited"])

    def test_returns_transcribed_text(self):
        eng = _make_engine_groq("안녕하세요")
        with patch("modules.stt.runtime_events.emit") as emit:
            result = eng._transcribe_groq(self._audio())
        self.assertEqual(result, "안녕하세요")
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "stt")
        self.assertEqual(emit.call_args.kwargs["status"], "success")
        self.assertTrue(emit.call_args.kwargs["request_sent"])
        self.assertEqual(emit.call_args.kwargs["text_len"], 5)

    def test_success_event_includes_prompt_budget(self):
        eng = _make_engine_groq("안녕하세요")
        with patch("modules.stt.runtime_events.emit") as emit:
            eng._transcribe_groq(self._audio())

        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["status"], "success")
        # A request was sent, so prompt-budget fields are populated (not None).
        self.assertIsInstance(kwargs["prompt_bytes"], int)
        self.assertIn("glossary_truncated", kwargs)
        self.assertIn("context_included", kwargs)

    def test_skipped_event_omits_prompt_budget(self):
        eng = _make_engine_groq()
        eng._groq_rate_limited_until = time.monotonic() + 60

        with patch("modules.stt.runtime_events.emit") as emit:
            self.assertIsNone(eng._transcribe_groq(self._audio()))

        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["status"], "skipped")
        # No request sent → no prompt was built → budget fields are None.
        self.assertIsNone(kwargs["prompt_bytes"])
        self.assertIsNone(kwargs["glossary_truncated"])

    def test_returns_none_for_empty_response(self):
        eng = _make_engine_groq("")
        self.assertIsNone(eng._transcribe_groq(self._audio()))

    def test_returns_none_for_whitespace_response(self):
        eng = _make_engine_groq("   ")
        self.assertIsNone(eng._transcribe_groq(self._audio()))

    def test_returns_none_on_exception(self):
        eng = _make_engine_groq()
        eng._groq_client.audio.transcriptions.create.side_effect = Exception("rate limit")
        self.assertIsNone(eng._transcribe_groq(self._audio()))

    def test_rate_limit_exception_warns_and_returns_none(self):
        class RateLimitError(Exception):
            status_code = 429

            def __str__(self):
                return "Error code: 429 - rate_limit_exceeded"

        eng = _make_engine_groq()
        eng._groq_client.audio.transcriptions.create.side_effect = RateLimitError()

        with self.assertLogs("stt", level="WARNING") as cm:
            with patch("modules.stt.runtime_events.emit") as emit:
                self.assertIsNone(eng._transcribe_groq(self._audio()))

        self.assertTrue(any("rate limited" in line for line in cm.output))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "stt")
        self.assertEqual(emit.call_args.kwargs["status"], "failed")
        self.assertEqual(emit.call_args.kwargs["reason"], "rate_limited")
        self.assertTrue(emit.call_args.kwargs["request_sent"])
        self.assertGreater(eng._groq_rate_limited_until, time.monotonic())

    def test_primary_rate_limit_retries_same_chunk_with_fallback_key(self):
        class RateLimitError(Exception):
            status_code = 429

            def __str__(self):
                return "Error code: 429 - rate_limit_exceeded"

        eng = _make_engine_groq()
        fallback_client = MagicMock()
        fallback_client.audio.transcriptions.create.return_value = _make_groq_resp("fallback result")
        eng._groq_fallback_client = fallback_client
        eng._groq_client.audio.transcriptions.create.side_effect = RateLimitError()

        with self.assertLogs("stt", level="WARNING") as cm:
            with patch("modules.stt.runtime_events.emit") as emit:
                result = eng._transcribe_groq(self._audio())

        self.assertEqual(result, "fallback result")
        self.assertTrue(any("retrying same chunk with fallback key" in line for line in cm.output))
        eng._groq_client.audio.transcriptions.create.assert_called_once()
        fallback_client.audio.transcriptions.create.assert_called_once()
        self.assertEqual(emit.call_count, 2)
        self.assertEqual(emit.call_args_list[0].kwargs["status"], "failed")
        self.assertEqual(emit.call_args_list[0].kwargs["reason"], "rate_limited")
        self.assertEqual(emit.call_args_list[0].kwargs["attempt_index"], 1)
        self.assertEqual(emit.call_args_list[0].kwargs["key_role"], "primary")
        self.assertTrue(emit.call_args_list[0].kwargs["will_retry"])
        self.assertEqual(emit.call_args_list[1].kwargs["status"], "success")
        self.assertEqual(emit.call_args_list[1].kwargs["attempt_index"], 2)
        self.assertEqual(emit.call_args_list[1].kwargs["key_role"], "fallback")
        self.assertFalse(emit.call_args_list[1].kwargs["will_retry"])
        self.assertGreater(eng._groq_rate_limited_until, time.monotonic())
        self.assertLessEqual(eng._groq_fallback_rate_limited_until, time.monotonic())
        self.assertTrue(eng._groq_prefer_fallback_key)

    def test_primary_error_does_not_retry_with_fallback_key(self):
        eng = _make_engine_groq()
        fallback_client = MagicMock()
        eng._groq_fallback_client = fallback_client
        eng._groq_client.audio.transcriptions.create.side_effect = RuntimeError("connection dropped")

        with self.assertLogs("stt", level="ERROR"):
            with patch("modules.stt.runtime_events.emit") as emit:
                result = eng._transcribe_groq(self._audio())

        self.assertIsNone(result)
        eng._groq_client.audio.transcriptions.create.assert_called_once()
        fallback_client.audio.transcriptions.create.assert_not_called()
        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["status"], "failed")
        self.assertEqual(emit.call_args.kwargs["reason"], "error")
        self.assertEqual(emit.call_args.kwargs["attempt_index"], 1)
        self.assertEqual(emit.call_args.kwargs["key_role"], "primary")
        self.assertFalse(emit.call_args.kwargs["will_retry"])

    def test_primary_rate_limit_switches_future_chunks_to_fallback_key(self):
        class RateLimitError(Exception):
            status_code = 429

            def __str__(self):
                return "Error code: 429 - rate_limit_exceeded"

        eng = _make_engine_groq()
        fallback_client = MagicMock()
        fallback_client.audio.transcriptions.create.side_effect = [
            _make_groq_resp("first fallback"),
            _make_groq_resp("second fallback"),
        ]
        eng._groq_fallback_client = fallback_client
        eng._groq_client.audio.transcriptions.create.side_effect = [
            RateLimitError(),
            AssertionError("primary should not be retried after fallback preference is set"),
        ]

        with patch("modules.stt.runtime_events.emit") as emit:
            self.assertEqual(eng._transcribe_groq(self._audio()), "first fallback")
            eng._groq_rate_limited_until = 0.0
            self.assertEqual(eng._transcribe_groq(self._audio()), "second fallback")

        self.assertEqual(eng._groq_client.audio.transcriptions.create.call_count, 1)
        self.assertEqual(fallback_client.audio.transcriptions.create.call_count, 2)
        self.assertEqual(emit.call_args.kwargs["attempt_index"], 1)
        self.assertEqual(emit.call_args.kwargs["key_role"], "fallback")
        self.assertFalse(emit.call_args.kwargs["will_retry"])

    def test_fallback_rate_limit_switches_back_to_primary_key(self):
        class RateLimitError(Exception):
            status_code = 429

            def __str__(self):
                return "Error code: 429 - rate_limit_exceeded"

        eng = _make_engine_groq("primary result")
        fallback_client = MagicMock()
        fallback_client.audio.transcriptions.create.side_effect = RateLimitError()
        eng._groq_fallback_client = fallback_client
        eng._groq_prefer_fallback_key = True

        with self.assertLogs("stt", level="WARNING") as cm:
            with patch("modules.stt.runtime_events.emit") as emit:
                result = eng._transcribe_groq(self._audio())

        self.assertEqual(result, "primary result")
        self.assertTrue(any("retrying same chunk with primary key" in line for line in cm.output))
        fallback_client.audio.transcriptions.create.assert_called_once()
        eng._groq_client.audio.transcriptions.create.assert_called_once()
        self.assertEqual(emit.call_count, 2)
        self.assertEqual(emit.call_args_list[0].kwargs["status"], "failed")
        self.assertEqual(emit.call_args_list[0].kwargs["reason"], "rate_limited")
        self.assertEqual(emit.call_args_list[0].kwargs["attempt_index"], 1)
        self.assertEqual(emit.call_args_list[0].kwargs["key_role"], "fallback")
        self.assertTrue(emit.call_args_list[0].kwargs["will_retry"])
        self.assertEqual(emit.call_args_list[1].kwargs["status"], "success")
        self.assertEqual(emit.call_args_list[1].kwargs["attempt_index"], 2)
        self.assertEqual(emit.call_args_list[1].kwargs["key_role"], "primary")
        self.assertFalse(emit.call_args_list[1].kwargs["will_retry"])
        self.assertFalse(eng._groq_prefer_fallback_key)

    def test_zero_cooldown_rate_limits_still_stop_after_one_cross_key_retry(self):
        from config import cfg

        class RateLimitError(Exception):
            status_code = 429

        eng = _make_engine_groq()
        fallback_client = MagicMock()
        eng._groq_fallback_client = fallback_client
        eng._groq_client.audio.transcriptions.create.side_effect = RateLimitError()
        fallback_client.audio.transcriptions.create.side_effect = RateLimitError()
        original_cooldown = cfg.stt.groq_rate_limit_cooldown_sec
        object.__setattr__(cfg.stt, "groq_rate_limit_cooldown_sec", 0.0)
        try:
            with patch("modules.stt.runtime_events.emit") as emit:
                result = eng._transcribe_groq(self._audio())
        finally:
            object.__setattr__(cfg.stt, "groq_rate_limit_cooldown_sec", original_cooldown)

        self.assertIsNone(result)
        eng._groq_client.audio.transcriptions.create.assert_called_once()
        fallback_client.audio.transcriptions.create.assert_called_once()
        self.assertEqual(emit.call_count, 2)
        self.assertEqual(
            [call.kwargs["attempt_index"] for call in emit.call_args_list],
            [1, 2],
        )
        self.assertEqual(
            [call.kwargs["will_retry"] for call in emit.call_args_list],
            [True, False],
        )

    def test_rate_limit_cooldown_skips_without_request(self):
        eng = _make_engine_groq()
        eng._groq_rate_limited_until = time.monotonic() + 60

        with patch("modules.stt.runtime_events.emit") as emit:
            self.assertIsNone(eng._transcribe_groq(self._audio()))

        eng._groq_client.audio.transcriptions.create.assert_not_called()
        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["status"], "skipped")
        self.assertEqual(emit.call_args.kwargs["reason"], "rate_limit_cooldown")
        self.assertFalse(emit.call_args.kwargs["request_sent"])
        self.assertEqual(emit.call_args.kwargs["attempt_index"], 1)
        self.assertEqual(emit.call_args.kwargs["key_role"], "none")
        self.assertFalse(emit.call_args.kwargs["will_retry"])

    def test_below_volume_emits_skipped_without_request(self):
        eng = _make_engine_groq()

        with patch("modules.stt.runtime_events.emit") as emit:
            self.assertIsNone(eng._transcribe_groq(np.zeros(16000, dtype=np.float32)))

        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["status"], "skipped")
        self.assertEqual(emit.call_args.kwargs["reason"], "below_volume_threshold")
        self.assertFalse(emit.call_args.kwargs["request_sent"])
        self.assertEqual(emit.call_args.kwargs["attempt_index"], 1)
        self.assertEqual(emit.call_args.kwargs["key_role"], "none")
        self.assertFalse(emit.call_args.kwargs["will_retry"])
        eng._groq_client.audio.transcriptions.create.assert_not_called()

    def test_normalizes_before_volume_skip(self):
        eng = _make_engine_groq("작게 말해도 들려요")
        quiet_audio = np.full(16000, 0.006, dtype=np.float32)

        with patch("modules.stt.runtime_events.emit") as emit:
            result = eng._transcribe_groq(quiet_audio)

        self.assertEqual(result, "작게 말해도 들려요")
        eng._groq_client.audio.transcriptions.create.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["status"], "success")
        self.assertTrue(emit.call_args.kwargs["request_sent"])
        self.assertLess(emit.call_args.kwargs["audio_rms"], 0.01)
        self.assertGreaterEqual(emit.call_args.kwargs["normalized_rms"], 0.01)

    def test_returns_none_when_groq_client_is_none(self):
        eng = _make_engine_groq()
        eng._groq_client = None
        self.assertIsNone(eng._transcribe_groq(self._audio()))

    def test_init_groq_uses_fail_fast_client_options(self):
        eng = STTEngine.__new__(STTEngine)
        eng._groq_client = None

        with patch("modules.stt.cfg") as mock_cfg, patch("groq.Groq") as groq_ctor:
            mock_cfg.keys.groq = "test-key"
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.groq_timeout = 10.0
            mock_cfg.stt.groq_max_retries = 0

            eng._init_groq()

        groq_ctor.assert_called_once_with(
            api_key="test-key",
            max_retries=0,
            timeout=10.0,
        )


# ---------------------------------------------------------------------------
# STTEngine.transcribe  fallback chain
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestTranscribeFallback(unittest.TestCase):

    def _audio(self):
        return np.full(1600, 0.1, dtype=np.float32)

    def test_uses_sensevoice_first(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response(
            "<|Speech|>테스트"
        )
        result = eng.transcribe(self._audio())
        self.assertEqual(result, "테스트")
        eng._sense_voice.generate.assert_called_once()

    def test_sensevoice_no_speech_does_not_switch_to_groq(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.return_value = _sv_response("<|BGM|>")
        groq_mock = MagicMock()
        groq_mock.audio.transcriptions.create.return_value = _make_groq_resp("Groq result")
        eng._groq_client = groq_mock

        with patch.object(eng, "_init_groq"):   # prevent real Groq init
            result = eng.transcribe(self._audio())

        self.assertIsNone(result)
        self.assertFalse(eng._use_groq)
        groq_mock.audio.transcriptions.create.assert_not_called()

    def test_falls_back_to_groq_when_sensevoice_errors(self):
        eng = _make_engine_sv()
        eng._sense_voice.generate.side_effect = RuntimeError("sensevoice crashed")
        groq_mock = MagicMock()
        groq_mock.audio.transcriptions.create.return_value = _make_groq_resp("Groq result")
        eng._groq_client = groq_mock

        with patch.object(eng, "_init_groq"):   # prevent real Groq init
            result = eng.transcribe(self._audio())

        self.assertEqual(result, "Groq result")
        self.assertTrue(eng._use_groq)

    def test_transcribe_event_includes_engine_and_profile(self):
        eng = _make_engine_groq("Groq result")

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "isegye_lilpa"
            mock_cfg.stt.groq_prompt = ""
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.max_japanese_chars = 2
            event = eng.transcribe_event(self._audio())

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "Groq result")
        self.assertEqual(event.engine, "groq")
        self.assertEqual(event.profile_id, "isegye_lilpa")

    def test_transcribe_event_mints_incrementing_utterance_id(self):
        eng = _make_engine_groq("Groq result")
        # Distinct transcripts so dedupe_transcript_overlap doesn't swallow the
        # second call as a repeat of the first.
        eng._groq_client.audio.transcriptions.create.side_effect = [
            _make_groq_resp("첫번째 문장"),
            _make_groq_resp("두번째 문장"),
        ]

        with patch("modules.stt.cfg") as mock_cfg, \
                patch("modules.stt.runtime_events.emit") as emit:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = ""
            mock_cfg.stt.groq_prompt = ""
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.max_japanese_chars = 2
            first = eng.transcribe_event(self._audio())
            first_emit_id = emit.call_args.kwargs["utterance_id"]
            second = eng.transcribe_event(self._audio())
            second_emit_id = emit.call_args.kwargs["utterance_id"]

        # Each transcription gets a fresh monotonic id, shared between the
        # returned TranscriptionEvent and its emitted stt runtime event.
        self.assertEqual(first.utterance_id, "utt-1")
        self.assertEqual(first_emit_id, "utt-1")
        self.assertEqual(second.utterance_id, "utt-2")
        self.assertEqual(second_emit_id, "utt-2")

    def test_transcribe_event_includes_groq_confidence_metadata(self):
        eng = _make_engine_groq("Groq result")
        eng._groq_client.audio.transcriptions.create.return_value = _make_groq_resp(
            "Groq result",
            segments=[
                {"start": 0.0, "end": 0.8, "text": "Groq", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0},
                {"start": 0.8, "end": 1.6, "text": "result", "avg_logprob": -0.4, "no_speech_prob": 0.3, "compression_ratio": 1.0},
            ],
        )

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = ""
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.max_japanese_chars = 2
            event = eng.transcribe_event(
                AudioChunk(
                    audio=self._audio(),
                    overlap_seconds=0.4,
                    vad_cut_reason="silence",
                    raw_audio_seconds=1.2,
                )
            )

        self.assertIsNotNone(event)
        self.assertAlmostEqual(event.avg_logprob, -0.3)
        self.assertAlmostEqual(event.no_speech_prob, 0.2)
        self.assertEqual(event.overlap_seconds, 0.4)
        self.assertEqual(event.vad_cut_reason, "silence")
        self.assertEqual(
            event.segments,
            (
                SegmentInfo(start=0.0, end=0.8, text="Groq", avg_logprob=-0.2, no_speech_prob=0.1),
                SegmentInfo(start=0.8, end=1.6, text="result", avg_logprob=-0.4, no_speech_prob=0.3),
            ),
        )

    def test_timestamp_dedupe_drops_fully_overlapped_segments(self):
        eng = _make_engine_groq("old words new words")
        eng._groq_client.audio.transcriptions.create.return_value = _make_groq_resp(
            "old words new words",
            segments=[
                {"start": 0.0, "end": 0.8, "text": "old words", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0},
                {"start": 0.9, "end": 1.4, "text": "new", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0},
                {"start": 1.4, "end": 1.8, "text": "words", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0},
            ],
        )

        with patch("modules.stt.cfg") as mock_cfg, patch("modules.stt.runtime_events.emit") as emit:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = ""
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.context_avg_logprob_threshold = -0.7
            mock_cfg.stt.context_no_speech_threshold = 0.3
            mock_cfg.stt.context_max_age_sec = 30.0
            mock_cfg.stt.context_min_chars = 4
            mock_cfg.stt.dedupe_by_timestamp = True
            mock_cfg.stt.max_japanese_chars = 2
            event = eng.transcribe_event(
                AudioChunk(audio=self._audio(), overlap_seconds=1.2, vad_cut_reason="soft_max_pause")
            )

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "new words")
        self.assertEqual([segment.text for segment in event.segments], ["new", "words"])
        self.assertEqual(emit.call_args.kwargs["timestamp_deduped_segments"], 1)
        self.assertEqual(emit.call_args.kwargs["timestamp_deduped_chars"], len("old words"))

    def test_timestamp_dedupe_can_be_disabled(self):
        eng = _make_engine_groq("old words new words")
        eng._groq_client.audio.transcriptions.create.return_value = _make_groq_resp(
            "old words new words",
            segments=[
                {"start": 0.0, "end": 0.8, "text": "old words", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0},
                {"start": 0.9, "end": 1.4, "text": "new words", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0},
            ],
        )

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = ""
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.context_avg_logprob_threshold = -0.7
            mock_cfg.stt.context_no_speech_threshold = 0.3
            mock_cfg.stt.context_max_age_sec = 30.0
            mock_cfg.stt.context_min_chars = 4
            mock_cfg.stt.dedupe_by_timestamp = False
            mock_cfg.stt.max_japanese_chars = 2
            event = eng.transcribe_event(
                AudioChunk(audio=self._audio(), overlap_seconds=1.2, vad_cut_reason="soft_max_pause")
            )

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "old words new words")
        self.assertEqual([segment.text for segment in event.segments], ["old words", "new words"])

    def test_transcribe_event_dedupes_overlap_against_previous_text(self):
        eng = _make_engine_groq("여기 기지를 우리가 이제 막아야 돼요")
        eng._last_transcript = "선택하면은 여기 기지를 우리가"

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = ""
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.max_japanese_chars = 2
            event = eng.transcribe_event(self._audio())

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "이제 막아야 돼요")


# ---------------------------------------------------------------------------
# STTEngine.transcribe — consecutive-None warning and SenseVoice recovery
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestTranscribeConsecutiveNone(unittest.TestCase):

    def _audio(self):
        return np.full(1600, 0.1, dtype=np.float32)

    def test_consecutive_none_counter_increments(self):
        eng = _make_engine_groq("")   # empty response → None
        eng._consecutive_none = 0
        eng.transcribe(self._audio())
        self.assertEqual(eng._consecutive_none, 1)

    def test_consecutive_none_resets_on_success(self):
        eng = _make_engine_groq("결과")
        eng._consecutive_none = 5
        eng.transcribe(self._audio())
        self.assertEqual(eng._consecutive_none, 0)

    def test_warning_logged_at_threshold(self):
        eng = _make_engine_groq("")
        eng._consecutive_none = _CONSECUTIVE_NONE_WARN - 1
        with self.assertLogs("stt", level="WARNING") as cm:
            eng.transcribe(self._audio())
        self.assertTrue(
            any("None" in line and str(_CONSECUTIVE_NONE_WARN) in line for line in cm.output),
            f"Expected warning about {_CONSECUTIVE_NONE_WARN} consecutive Nones",
        )


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestSenseVoiceRecoveryProbe(unittest.TestCase):

    def _audio(self):
        return np.full(1600, 0.1, dtype=np.float32)

    def test_recovery_probe_fires_at_interval(self):
        """After PROBE_EVERY Groq calls, a SenseVoice probe should happen."""
        eng = _make_engine_groq("groq result")
        eng._sense_voice = MagicMock()
        # SenseVoice returns valid speech on the probe
        eng._sense_voice.generate.return_value = _sv_response(
            "<|Speech|>복구됨"
        )
        eng._sv_fallback_counter = _SENSEVOICE_PROBE_EVERY - 1

        result = eng.transcribe(self._audio())

        # SenseVoice should have been called exactly once (the probe)
        eng._sense_voice.generate.assert_called_once()
        # Engine should switch back from Groq
        self.assertFalse(eng._use_groq)
        self.assertEqual(result, "복구됨")

    def test_recovery_probe_skips_when_sense_voice_is_none(self):
        """If SenseVoice was never loaded, no probe attempt."""
        eng = _make_engine_groq("groq result")
        eng._sense_voice = None
        eng._sv_fallback_counter = _SENSEVOICE_PROBE_EVERY - 1

        result = eng.transcribe(self._audio())

        self.assertEqual(result, "groq result")
        self.assertTrue(eng._use_groq)

    def test_failed_probe_keeps_groq_active(self):
        """If SenseVoice probe returns None, stay on Groq."""
        eng = _make_engine_groq("groq result")
        eng._sense_voice = MagicMock()
        eng._sense_voice.generate.return_value = _sv_response("<|BGM|>")
        eng._sv_fallback_counter = _SENSEVOICE_PROBE_EVERY - 1

        result = eng.transcribe(self._audio())

        self.assertTrue(eng._use_groq)
        self.assertEqual(result, "groq result")


# ---------------------------------------------------------------------------
# STT thread: start()  (mocked engine, numpy required)
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class TestSttThread(unittest.TestCase):

    def _audio(self):
        return np.zeros(1600, dtype=np.float32)

    def test_audio_queue_to_text_queue(self):
        from modules.stt import start as stt_start
        audio_q: queue.Queue = queue.Queue()
        text_q: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch("modules.stt.STTEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.transcribe_event.return_value = TranscriptionEvent(
                text="테스트",
                engine="groq",
                profile_id="hades_chxxnnx",
            )
            stt_start(audio_q, text_q, stop)
            audio_q.put(self._audio())
            result = text_q.get(timeout=3)
            stop.set()

        self.assertEqual(result.text, "테스트")
        self.assertEqual(result.engine, "groq")

    def test_audio_dump_writes_wav_when_enabled(self):
        import tempfile
        from pathlib import Path
        from config import cfg
        from modules.stt import start as stt_start

        audio_q: queue.Queue = queue.Queue()
        text_q: queue.Queue = queue.Queue()
        stop = threading.Event()
        tmp = Path(tempfile.mkdtemp())

        object.__setattr__(cfg.stt, "dump_audio", True)
        try:
            with patch("modules.stt.STTEngine") as MockEngine, \
                    patch("modules.stt._AUDIO_DUMP_ROOT", tmp):
                instance = MockEngine.return_value
                instance.available = True
                instance.transcribe_event.return_value = TranscriptionEvent(
                    text="테스트", engine="groq", profile_id="", utterance_id="utt-1",
                )
                stt_start(audio_q, text_q, stop)
                audio_q.put(self._audio())
                text_q.get(timeout=3)
                stop.set()
        finally:
            object.__setattr__(cfg.stt, "dump_audio", False)

        wavs = list(tmp.glob("**/utt-1.wav"))
        self.assertEqual(len(wavs), 1, "expected one dumped wav named by utterance_id")

    def test_none_transcription_not_forwarded(self):
        from modules.stt import start as stt_start
        audio_q: queue.Queue = queue.Queue()
        text_q: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch("modules.stt.STTEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.transcribe_event.return_value = None
            stt_start(audio_q, text_q, stop)
            audio_q.put(self._audio())
            time.sleep(0.3)
            stop.set()

        self.assertTrue(text_q.empty())

    def test_pause_prevents_processing(self):
        from modules.stt import start as stt_start
        audio_q: queue.Queue = queue.Queue()
        text_q: queue.Queue = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()

        with patch("modules.stt.STTEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.transcribe_event.return_value = TranscriptionEvent(
                text="should not appear",
                engine="groq",
                profile_id="hades_chxxnnx",
            )
            stt_start(audio_q, text_q, stop, pause_event=pause)
            audio_q.put(self._audio())
            time.sleep(0.4)
            stop.set()

        self.assertTrue(text_q.empty())

    def test_stop_event_exits_cleanly(self):
        from modules.stt import start as stt_start
        audio_q: queue.Queue = queue.Queue()
        text_q: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch("modules.stt.STTEngine"):
            t = stt_start(audio_q, text_q, stop)
            stop.set()
            t.join(timeout=3)

        self.assertFalse(t.is_alive())

    def test_thread_sets_stop_event_when_engine_unavailable(self):
        from modules.stt import start as stt_start
        audio_q: queue.Queue = queue.Queue()
        text_q: queue.Queue = queue.Queue()
        stop = threading.Event()

        with patch("modules.stt.STTEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.available = False
            t = stt_start(audio_q, text_q, stop)
            t.join(timeout=3)

        self.assertFalse(t.is_alive(), "STT thread should exit when engine unavailable")
        self.assertTrue(stop.is_set(), "STT thread should signal shutdown")
        self.assertTrue(text_q.empty())


class TestGroqPromptBuilder(unittest.TestCase):

    def test_build_groq_prompt_includes_seed_glossary_and_recent_context(self):
        eng = _make_engine_groq("ignored")
        eng._last_transcript = "  previous   line with   extra spaces  "
        eng._last_context_transcript = "  previous   line with   extra spaces  "
        eng._last_context_updated_at = time.monotonic()

        with patch("modules.stt.cfg") as mock_cfg, \
            patch("modules.stt.build_stt_glossary", return_value="profile terms") as glossary:
            mock_cfg.stt.groq_prompt = "  custom prompt words  "
            mock_cfg.stt.use_profile_glossary = True
            mock_cfg.active_streamer_profile = "isegye_lilpa"
            mock_cfg.translation.current_activity = ""
            prompt = eng._build_groq_prompt()

        self.assertEqual(
            prompt,
            "custom prompt words\nprofile terms\nRecent Korean transcript context: previous line with extra spaces",
        )
        glossary.assert_called_once_with("isegye_lilpa", extra_terms=())

    def test_build_groq_prompt_can_skip_profile_glossary(self):
        eng = _make_engine_groq("ignored")
        eng._last_transcript = "recent context"
        eng._last_context_transcript = "recent context"
        eng._last_context_updated_at = time.monotonic()

        with patch("modules.stt.cfg") as mock_cfg, \
             patch("modules.stt.build_stt_glossary") as glossary:
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = False
            prompt = eng._build_groq_prompt()

        self.assertEqual(prompt, "seed prompt\nRecent Korean transcript context: recent context")
        glossary.assert_not_called()

    def test_build_groq_prompt_uses_context_transcript_not_dedupe_transcript(self):
        eng = _make_engine_groq("ignored")
        eng._last_transcript = "low confidence transcript"
        eng._last_context_transcript = ""
        eng._last_context_gate_reason = "avg_logprob"

        with patch("modules.stt.cfg") as mock_cfg, \
             patch("modules.stt.build_stt_glossary") as glossary:
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = False
            prompt = eng._build_groq_prompt()

        self.assertEqual(prompt, "seed prompt")
        self.assertTrue(eng._last_prompt_context_gated)
        self.assertEqual(eng._last_prompt_context_gate_reason, "avg_logprob")
        glossary.assert_not_called()

    def test_build_groq_prompt_expires_old_context(self):
        eng = _make_engine_groq("ignored")
        eng._last_transcript = "old transcript"
        eng._last_context_transcript = "old transcript"
        eng._last_context_updated_at = time.monotonic() - 60

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.context_max_age_sec = 30.0
            prompt = eng._build_groq_prompt()

        self.assertEqual(prompt, "seed prompt")
        self.assertEqual(eng._last_context_transcript, "")
        self.assertTrue(eng._last_prompt_context_gated)
        self.assertEqual(eng._last_prompt_context_gate_reason, "expired")

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed")
    def test_transcribe_groq_passes_structured_prompt(self):
        eng = _make_engine_groq("transcribed")
        eng._last_transcript = "recent context"
        eng._last_context_transcript = "recent context"
        eng._last_context_updated_at = time.monotonic()
        audio = np.full(16000, 0.1, dtype=np.float32)

        with patch("modules.stt.cfg") as mock_cfg, \
             patch("modules.stt.build_stt_glossary", return_value="profile terms"):
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = True
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.max_japanese_chars = 2
            mock_cfg.active_streamer_profile = "isegye_lilpa"
            result = eng._transcribe_groq(audio)

        self.assertEqual(result, "transcribed")
        sent_prompt = eng._groq_client.audio.transcriptions.create.call_args.kwargs["prompt"]
        self.assertEqual(
            sent_prompt,
            "seed prompt\nprofile terms\nRecent Korean transcript context: recent context",
        )

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed")
    def test_good_groq_result_becomes_next_prompt_context(self):
        eng = _make_engine_groq()
        audio = np.full(16000, 0.1, dtype=np.float32)
        eng._groq_client.audio.transcriptions.create.side_effect = [
            _make_groq_resp(
                "첫번째 문장",
                segments=[{"avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0}],
            ),
            _make_groq_resp(
                "두번째 문장",
                segments=[{"avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0}],
            ),
        ]

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.context_avg_logprob_threshold = -0.7
            mock_cfg.stt.context_no_speech_threshold = 0.3
            mock_cfg.stt.context_max_age_sec = 30.0
            mock_cfg.stt.context_min_chars = 4
            mock_cfg.stt.max_japanese_chars = 2
            first = eng.transcribe_event(audio)
            second = eng.transcribe_event(audio)

        self.assertEqual(first.text, "첫번째 문장")
        self.assertEqual(second.text, "두번째 문장")
        second_prompt = eng._groq_client.audio.transcriptions.create.call_args_list[1].kwargs["prompt"]
        self.assertEqual(
            second_prompt,
            "seed prompt\nRecent Korean transcript context: 첫번째 문장",
        )

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed")
    def test_low_confidence_groq_result_is_not_next_prompt_context(self):
        eng = _make_engine_groq()
        audio = np.full(16000, 0.1, dtype=np.float32)
        eng._groq_client.audio.transcriptions.create.side_effect = [
            _make_groq_resp(
                "첫번째 문장",
                segments=[{"avg_logprob": -0.8, "no_speech_prob": 0.1, "compression_ratio": 1.0}],
            ),
            _make_groq_resp(
                "두번째 문장",
                segments=[{"avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 1.0}],
            ),
        ]

        with patch("modules.stt.cfg") as mock_cfg, patch("modules.stt.runtime_events.emit") as emit:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.context_avg_logprob_threshold = -0.7
            mock_cfg.stt.context_no_speech_threshold = 0.3
            mock_cfg.stt.context_max_age_sec = 30.0
            mock_cfg.stt.context_min_chars = 4
            mock_cfg.stt.max_japanese_chars = 2
            first = eng.transcribe_event(audio)
            second = eng.transcribe_event(audio)

        self.assertEqual(first.text, "첫번째 문장")
        self.assertEqual(second.text, "두번째 문장")
        second_prompt = eng._groq_client.audio.transcriptions.create.call_args_list[1].kwargs["prompt"]
        self.assertEqual(second_prompt, "seed prompt")
        self.assertTrue(emit.call_args_list[1].kwargs["context_gated"])
        self.assertEqual(emit.call_args_list[1].kwargs["context_gate_reason"], "avg_logprob")

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed")
    def test_filtered_groq_result_clears_prompt_context(self):
        eng = _make_engine_groq()
        audio = np.full(16000, 0.1, dtype=np.float32)
        eng._last_transcript = "좋은 이전 문장"
        eng._last_context_transcript = "좋은 이전 문장"
        eng._last_context_updated_at = time.monotonic()
        eng._groq_client.audio.transcriptions.create.return_value = _make_groq_resp(
            "잡음",
            segments=[{"avg_logprob": -0.1, "no_speech_prob": 0.9, "compression_ratio": 1.0}],
        )

        with patch("modules.stt.cfg") as mock_cfg:
            mock_cfg.audio.volume_threshold = 0.01
            mock_cfg.audio.sample_rate = 16000
            mock_cfg.active_streamer_profile = "hades_chxxnnx"
            mock_cfg.stt.groq_prompt = "seed prompt"
            mock_cfg.stt.use_profile_glossary = False
            mock_cfg.stt.groq_model = "whisper-large-v3"
            mock_cfg.stt.language = "ko"
            mock_cfg.stt.no_speech_threshold = 0.6
            mock_cfg.stt.avg_logprob_threshold = -1.0
            mock_cfg.stt.context_avg_logprob_threshold = -0.7
            mock_cfg.stt.context_no_speech_threshold = 0.3
            mock_cfg.stt.context_max_age_sec = 30.0
            mock_cfg.stt.context_min_chars = 4
            mock_cfg.stt.max_japanese_chars = 2
            event = eng.transcribe_event(audio)

        self.assertIsNone(event)
        self.assertEqual(eng._last_context_transcript, "")
        self.assertEqual(eng._last_context_gate_reason, "filtered_no_speech_prob")


class TestStreamerSttGlossary(unittest.TestCase):

    def test_builds_profile_specific_stt_glossary(self):
        from modules.streamer_profiles import build_stt_glossary

        glossary = build_stt_glossary("isegye_lilpa")

        self.assertIn("\uc2a4\ud0c0\ub808\uc77c", glossary)
        self.assertIn("\ub9b4\ud30c", glossary)

    def test_can_build_profile_only_glossary(self):
        from modules.streamer_profiles import build_stt_glossary

        glossary = build_stt_glossary("isegye_lilpa", include_common=False)

        self.assertNotIn("\uc2a4\ud0c0\ub808\uc77c", glossary)
        self.assertIn("\ub9b4\ud30c", glossary)

    def test_unknown_profile_uses_common_terms(self):
        from modules.streamer_profiles import build_stt_glossary

        glossary = build_stt_glossary("unknown")

        self.assertIn("\uc2a4\ud0c0\ub808\uc77c", glossary)
        self.assertNotIn("\ub9b4\ud30c", glossary)

    def test_extra_stt_terms_are_prioritized_before_static_terms(self):
        from modules.streamer_profiles import build_stt_glossary

        glossary = build_stt_glossary(
            "hades_chxxnnx",
            extra_terms=("메가진화", "포켓몬"),
        )

        self.assertLess(glossary.index("메가진화"), glossary.index("스타레일"))
        self.assertLess(glossary.index("포켓몬"), glossary.index("하데스"))

    def test_known_profile_ids_include_configured_profiles(self):
        from modules.streamer_profiles import known_profile_ids

        self.assertIn("stellive_hina", known_profile_ids())
        self.assertIn("hades_chxxnnx", known_profile_ids())


if __name__ == "__main__":
    unittest.main()
