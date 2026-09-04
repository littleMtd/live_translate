from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from modules.pipeline_events import AudioChunk
from modules.stt import STTEngine


def _engine(text: str = "안녕하세요") -> STTEngine:
    engine = STTEngine.__new__(STTEngine)
    engine._sense_voice = None
    engine._elevenlabs_client = MagicMock()
    engine._elevenlabs_client.speech_to_text.convert.return_value = SimpleNamespace(
        text=text,
        language_code="ko",
        words=[SimpleNamespace(start=0.0, end=0.5, text=text)],
    )
    engine._groq_client = MagicMock()
    engine._groq_fallback_client = None
    engine._use_elevenlabs = True
    engine._use_groq = False
    engine._consecutive_none = 0
    engine._sv_fallback_counter = 0
    engine._groq_rate_limited_until = 0.0
    engine._groq_fallback_rate_limited_until = 0.0
    engine._groq_prefer_fallback_key = False
    engine._last_transcript = ""
    engine._last_context_transcript = ""
    engine._last_context_updated_at = None
    engine._last_context_source = None
    engine._current_context_provenance = None
    engine._last_context_gate_reason = ""
    engine._last_prompt_context_gated = False
    engine._last_prompt_context_gate_reason = ""
    engine._utterance_seq = 0
    engine._current_utterance_id = ""
    engine._last_audio_seconds = 0.0
    engine._last_segments = ()
    engine._last_timestamp_deduped_segments = 0
    engine._last_timestamp_deduped_chars = 0
    engine._current_overlap_seconds = 0.0
    engine._current_vad_cut_reason = ""
    engine._last_prompt_budget = None
    engine._last_sensevoice_error = False
    engine._last_elevenlabs_error = False
    engine._elevenlabs_retry_after = 0.0
    engine._last_elevenlabs_keyterm_count = 0
    engine._last_detected_language = ""
    engine._last_foreign_speech_allowed = False
    return engine


def _cfg() -> MagicMock:
    config = MagicMock()
    config.audio.volume_threshold = 0.01
    config.audio.sample_rate = 16000
    config.audio.stt_normalize_enabled = False
    config.active_streamer_profile = "irise"
    config.translation.current_activity = ""
    config.translation.translate_coherent_foreign_speech = False
    config.stt.elevenlabs_model = "scribe_v2"
    config.stt.elevenlabs_timeout = 15.0
    config.stt.elevenlabs_max_keyterms = 100
    config.stt.elevenlabs_failure_cooldown_sec = 30.0
    config.stt.use_profile_glossary = True
    config.stt.language = "ko"
    config.stt.max_japanese_chars = 2
    config.stt.max_repeat_ratio = 0.7
    config.stt.context_min_chars = 4
    config.stt.groq_model = "whisper-large-v3"
    config.stt.groq_prompt = ""
    config.stt.groq_rate_limit_cooldown_sec = 30.0
    config.stt.no_speech_threshold = 0.6
    config.stt.avg_logprob_threshold = -1.0
    return config


def _audio() -> np.ndarray:
    return np.full(16000, 0.1, dtype=np.float32)


def _profile_snapshot(common=(), terms=()):
    registry = SimpleNamespace(
        common_stt_terms=tuple(common),
        terms_for=lambda _profile_id: tuple(terms),
    )
    return SimpleNamespace(
        generation=7,
        evidence_source="scene_vision",
        source_profile_id="irise",
        effective_profile_id="irise",
        stt_glossary_applied=True,
        registry=registry,
        as_metadata=lambda: {"profile_id": "irise", "profile_generation": 7},
    )


def test_scribe_v2_success_returns_elevenlabs_event_and_observability():
    engine = _engine("아이리즈 하트 크러쉬")
    config = _cfg()

    snapshot = _profile_snapshot(("공통어",), ("아이리즈", "하트 크러쉬"))
    with patch("modules.stt.cfg", config), \
            patch("modules.stt.profile_state.current", return_value=snapshot), \
            patch("modules.stt.runtime_events.emit") as emit:
        event = engine.transcribe_event(_audio())

    assert event is not None
    assert event.engine == "elevenlabs"
    assert event.text == "아이리즈 하트 크러쉬"
    request = engine._elevenlabs_client.speech_to_text.convert.call_args.kwargs
    assert request["model_id"] == "scribe_v2"
    assert request["language_code"] == "ko"
    assert request["keyterms"] == ["공통어", "아이리즈", "하트 크러쉬"]
    assert emit.call_args.kwargs["engine"] == "elevenlabs"
    assert emit.call_args.kwargs["status"] == "success"
    assert emit.call_args.kwargs["keyterm_count"] == 3
    assert "keyterms" not in emit.call_args.kwargs


def test_scribe_v2_removes_first_proven_cross_boundary_overlap():
    engine = _engine()
    engine._last_transcript = "백 개를 내가 핀볼 공에 넣고 싶어 그러면은"
    engine._current_overlap_seconds = 1.0
    engine._current_overlap_represented = True
    engine._elevenlabs_client.speech_to_text.convert.return_value = SimpleNamespace(
        text="싶어. 그러면은 백 개를 열 개 해서 천 개를 넣어주시면 되는데",
        language_code="ko",
        words=[
            SimpleNamespace(start=0.0, end=0.42, text="싶어."),
            SimpleNamespace(start=0.43, end=0.86, text=" 그러면은"),
            SimpleNamespace(start=1.02, end=1.35, text=" 백 개를"),
            SimpleNamespace(start=1.36, end=2.4, text=" 열 개 해서 천 개를 넣어주시면 되는데"),
        ],
    )
    config = _cfg()

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit") as emit:
        text = engine._transcribe_elevenlabs(_audio())

    assert text == "백 개를 열 개 해서 천 개를 넣어주시면 되는데"
    assert [segment.text for segment in engine._last_segments] == [
        " 백 개를",
        " 열 개 해서 천 개를 넣어주시면 되는데",
    ]
    assert emit.call_args.kwargs["timestamp_deduped_segments"] == 2
    assert emit.call_args.kwargs["timestamp_deduped_chars"] == len("싶어.") + len(" 그러면은")


def test_scribe_v2_removes_second_proven_cross_boundary_overlap():
    engine = _engine()
    engine._last_transcript = "뭐 몇 개요, 몇 개요 이렇게 말씀해 주셔야 돼요."
    engine._current_overlap_seconds = 1.2
    engine._current_overlap_represented = True
    engine._elevenlabs_client.speech_to_text.convert.return_value = SimpleNamespace(
        text="몇 개요 이렇게 말씀해 주셔야 돼요. 그리고 리롤 있습니다.",
        language_code="ko",
        words=[
            SimpleNamespace(start=0.0, end=0.3, text="몇 개요"),
            SimpleNamespace(start=0.31, end=0.65, text=" 이렇게"),
            SimpleNamespace(start=0.66, end=1.12, text=" 말씀해 주셔야 돼요."),
            SimpleNamespace(start=1.24, end=2.1, text=" 그리고 리롤 있습니다."),
        ],
    )
    config = _cfg()

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit"):
        text = engine._transcribe_elevenlabs(_audio())

    assert text == "그리고 리롤 있습니다."


def test_scribe_timestamps_preserve_intentional_repetition_after_overlap_window():
    engine = _engine()
    engine._last_transcript = "다시 다시 해 봐."
    engine._current_overlap_seconds = 1.0
    engine._current_overlap_represented = True
    engine._elevenlabs_client.speech_to_text.convert.return_value = SimpleNamespace(
        text="다시 다시 해 봐.",
        language_code="ko",
        words=[
            SimpleNamespace(start=1.08, end=1.4, text="다시"),
            SimpleNamespace(start=1.41, end=1.75, text=" 다시"),
            SimpleNamespace(start=1.76, end=2.2, text=" 해 봐."),
        ],
    )
    config = _cfg()

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit") as emit:
        text = engine._transcribe_elevenlabs(_audio())

    assert text == "다시 다시 해 봐."
    assert emit.call_args.kwargs["timestamp_deduped_segments"] == 0


def test_overlap_is_kept_when_preceding_audio_was_not_successfully_represented():
    engine = _engine()
    engine._elevenlabs_client.speech_to_text.convert.side_effect = [
        SimpleNamespace(text="", language_code="ko", words=[]),
        SimpleNamespace(
            text="유일한 앞말 뒤의 새말",
            language_code="ko",
            words=[
                SimpleNamespace(start=0.0, end=0.7, text="유일한 앞말"),
                SimpleNamespace(start=None, end=None, text=" ", type="spacing"),
                SimpleNamespace(start=1.05, end=1.6, text="뒤의 새말"),
            ],
        ),
    ]
    config = _cfg()
    first_audio = np.linspace(0.05, 0.15, 16000, dtype=np.float32)
    copied_prefix = first_audio[-16000:].copy()
    second_audio = np.concatenate(
        [copied_prefix, np.linspace(0.16, 0.2, 8000, dtype=np.float32)]
    )

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit"):
        first = engine.transcribe_event(first_audio)
        second = engine.transcribe_event(
            AudioChunk(audio=second_audio, overlap_seconds=1.0)
        )

    assert first is None
    assert second is not None
    assert second.text == "유일한 앞말 뒤의 새말"
    assert engine._last_timestamp_deduped_segments == 0


def test_overlap_is_removed_when_audio_prefix_matches_preceding_success():
    engine = _engine()
    engine._elevenlabs_client.speech_to_text.convert.side_effect = [
        SimpleNamespace(
            text="이미 나온 말",
            language_code="ko",
            words=[SimpleNamespace(start=0.0, end=0.8, text="이미 나온 말")],
        ),
        SimpleNamespace(
            text="이미 나온 말 새로운 말",
            language_code="ko",
            words=[
                SimpleNamespace(start=0.0, end=0.8, text="이미 나온 말"),
                SimpleNamespace(start=None, end=None, text=" ", type="spacing"),
                SimpleNamespace(start=1.05, end=1.6, text="새로운 말"),
            ],
        ),
    ]
    config = _cfg()
    first_audio = np.linspace(0.05, 0.15, 16000, dtype=np.float32)
    second_audio = np.concatenate(
        [first_audio.copy(), np.linspace(0.16, 0.2, 8000, dtype=np.float32)]
    )

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit"):
        first = engine.transcribe_event(first_audio)
        second = engine.transcribe_event(
            AudioChunk(audio=second_audio, overlap_seconds=1.0)
        )

    assert first is not None
    assert second is not None
    assert second.text == "새로운 말"
    assert engine._last_timestamp_deduped_segments == 1


def test_text_fallback_keeps_stale_match_after_failed_intermediary_chunk():
    engine = _engine()
    engine._elevenlabs_client.speech_to_text.convert.side_effect = [
        SimpleNamespace(text="반복되는 말입니다", language_code="ko", words=[]),
        SimpleNamespace(text="", language_code="ko", words=[]),
        # No word timestamps forces the exact-text compatibility path.
        SimpleNamespace(text="반복되는 말입니다", language_code="ko", words=[]),
    ]
    config = _cfg()
    first_audio = np.linspace(0.05, 0.15, 16000, dtype=np.float32)
    second_audio = np.concatenate(
        [first_audio.copy(), np.linspace(0.16, 0.2, 8000, dtype=np.float32)]
    )
    third_audio = np.concatenate(
        [second_audio[-16000:].copy(), np.linspace(0.21, 0.25, 8000, dtype=np.float32)]
    )

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit"):
        first = engine.transcribe_event(first_audio)
        failed = engine.transcribe_event(
            AudioChunk(audio=second_audio, overlap_seconds=1.0)
        )
        third = engine.transcribe_event(
            AudioChunk(audio=third_audio, overlap_seconds=1.0)
        )

    assert first is not None
    assert failed is None
    assert third is not None
    assert third.text == "반복되는 말입니다"


def test_provider_failure_retries_same_chunk_with_sequential_attempt_index():
    engine = _engine()
    engine._elevenlabs_client.speech_to_text.convert.side_effect = RuntimeError("outage")
    engine._groq_client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="Groq fallback",
        language="ko",
        segments=[],
    )
    config = _cfg()
    config.stt.use_profile_glossary = False

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.common_stt_terms", return_value=()), \
            patch("modules.stt.profile_stt_terms", return_value=()), \
            patch("modules.stt.runtime_events.emit") as emit:
        event = engine.transcribe_event(_audio())

    assert event is not None
    assert event.engine == "groq"
    assert event.text == "Groq fallback"
    assert engine._use_elevenlabs is True
    assert engine._use_groq is False
    assert engine._elevenlabs_retry_after > 0
    assert [call.kwargs["attempt_index"] for call in emit.call_args_list] == [1, 2]


def test_elevenlabs_then_groq_cross_key_retry_uses_attempts_one_two_three():
    class RateLimitError(Exception):
        status_code = 429

    engine = _engine()
    engine._elevenlabs_client.speech_to_text.convert.side_effect = RuntimeError("outage")
    engine._groq_client.audio.transcriptions.create.side_effect = RateLimitError("limited")
    engine._groq_fallback_client = MagicMock()
    engine._groq_fallback_client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="Fallback key result",
        language="ko",
        segments=[],
    )
    config = _cfg()
    config.stt.use_profile_glossary = False

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.common_stt_terms", return_value=()), \
            patch("modules.stt.profile_stt_terms", return_value=()), \
            patch("modules.stt.runtime_events.emit") as emit:
        event = engine.transcribe_event(_audio())

    assert event is not None
    assert event.engine == "groq"
    assert event.text == "Fallback key result"
    assert [call.kwargs["attempt_index"] for call in emit.call_args_list] == [1, 2, 3]


def test_filtered_empty_response_does_not_fallback_to_second_provider():
    engine = _engine("")
    engine._transcribe_groq = MagicMock(return_value="invented fallback")
    config = _cfg()

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.common_stt_terms", return_value=()), \
            patch("modules.stt.profile_stt_terms", return_value=()), \
            patch("modules.stt.runtime_events.emit"):
        event = engine.transcribe_event(_audio())

    assert event is None
    engine._transcribe_groq.assert_not_called()


def test_keyterm_filter_is_bounded_and_drops_unsupported_entries():
    engine = _engine()
    config = _cfg()
    config.stt.elevenlabs_max_keyterms = 2
    engine._current_profile_snapshot = _profile_snapshot(("common",), ("profile",))

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.terms_for_activity", return_value=("scene", "bad[term]")):
        terms = engine._elevenlabs_keyterms()

    assert terms == ["scene", "common"]


def test_scribe_metadata_is_retained_without_raw_identifier_telemetry():
    engine = _engine("first")
    engine._elevenlabs_client.speech_to_text.convert.return_value = SimpleNamespace(
        text="first",
        language_code="ko",
        language_probability=0.97,
        transcription_id="scribe-test-id",
        words=[SimpleNamespace(
            start=0.0,
            end=0.5,
            text="first",
            logprob=-0.3,
            type="word",
            speaker_id="speaker_0",
        )],
    )
    config = _cfg()
    config.stt.use_profile_glossary = False

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit") as emit:
        event = engine.transcribe_event(_audio())

    assert event is not None
    assert event.language_probability == 0.97
    assert event.transcription_id == "scribe-test-id"
    assert event.segments[0].logprob == -0.3
    assert event.segments[0].word_type == "word"
    assert event.segments[0].speaker_id == "speaker_0"
    assert emit.call_args.kwargs["language_probability"] == 0.97
    assert emit.call_args.kwargs["word_count"] == 1
    assert emit.call_args.kwargs["min_word_logprob"] == -0.3
    assert emit.call_args.kwargs["mean_word_logprob"] == -0.3
    assert emit.call_args.kwargs["word_type_counts"] == {"word": 1}
    assert "transcription_id" not in emit.call_args.kwargs


def test_missing_or_nonfinite_scribe_confidence_is_safe_and_not_stale():
    engine = _engine("first")
    first = SimpleNamespace(
        text="first",
        language_code="ko",
        language_probability=0.9,
        transcription_id="first-id",
        words=[SimpleNamespace(start=0.0, end=0.5, text="first", logprob=-0.2)],
    )
    second = SimpleNamespace(
        text="second",
        language_code="ko",
        language_probability=float("nan"),
        words=[SimpleNamespace(start=0.0, end=0.5, text="second", logprob=float("nan"))],
    )
    engine._elevenlabs_client.speech_to_text.convert.side_effect = [first, second]
    config = _cfg()
    config.stt.use_profile_glossary = False

    with patch("modules.stt.cfg", config), \
            patch("modules.stt.runtime_events.emit") as emit:
        first_event = engine.transcribe_event(_audio())
        second_event = engine.transcribe_event(_audio())

    assert first_event is not None and first_event.language_probability == 0.9
    assert second_event is not None
    assert second_event.language_probability is None
    assert second_event.transcription_id == ""
    assert second_event.segments[0].logprob is None
    assert emit.call_args.kwargs["language_probability"] is None
    assert emit.call_args.kwargs["min_word_logprob"] is None
    assert emit.call_args.kwargs["mean_word_logprob"] is None
