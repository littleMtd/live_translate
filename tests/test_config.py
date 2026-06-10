import unittest
from config import cfg


class TestConfig(unittest.TestCase):

    def test_cfg_instantiates(self):
        self.assertIsNotNone(cfg)

    def test_all_groups_present(self):
        self.assertIsNotNone(cfg.keys)
        self.assertIsNotNone(cfg.audio)
        self.assertIsNotNone(cfg.stt)
        self.assertIsNotNone(cfg.splitter)
        self.assertIsNotNone(cfg.translation)
        self.assertIsNotNone(cfg.subtitle)

    def test_audio_defaults(self):
        self.assertEqual(cfg.audio.sample_rate, 16000)
        self.assertEqual(cfg.audio.channels, 1)
        self.assertGreaterEqual(cfg.audio.capture_channels, cfg.audio.channels)
        self.assertGreater(cfg.audio.chunk_seconds, 0)
        self.assertGreater(cfg.audio.volume_threshold, 0)

    def test_splitter_ordering(self):
        # force cut 必須大於 min wait
        self.assertGreater(cfg.splitter.force_cut_seconds,
                           cfg.splitter.min_wait_seconds)

    def test_translation_engine_chain_valid(self):
        from config import _VALID_ENGINE_NAMES

        self.assertIsInstance(cfg.translation.engine_chain, tuple)
        for name in cfg.translation.engine_chain:
            self.assertIn(name, _VALID_ENGINE_NAMES)

    def test_streamer_profile_ids_match_registry(self):
        from config import _VALID_STREAMER_PROFILES
        from modules.streamer_profiles import known_profile_ids

        self.assertEqual(_VALID_STREAMER_PROFILES, known_profile_ids())

    def test_active_streamer_profile_matches_translation_profile(self):
        self.assertEqual(cfg.active_streamer_profile, cfg.translation.streamer_profile)

    def test_translation_slang_is_dict(self):
        from collections.abc import Mapping
        self.assertIsInstance(cfg.translation.slang, Mapping)
        self.assertGreater(len(cfg.translation.slang), 0)

    def test_default_slang_loads_from_data_file(self):
        from config import _DEFAULT_SLANG_PATH, _load_default_slang

        self.assertTrue(_DEFAULT_SLANG_PATH.exists())
        loaded = _load_default_slang()
        self.assertEqual(dict(loaded), dict(cfg.translation.slang))

    def test_default_slang_contains_runtime_quality_terms(self):
        self.assertEqual(cfg.translation.slang["마가 뜨다"], "冷場")
        self.assertEqual(cfg.translation.slang["붕 뜨는 시간"], "空掉的時間")
        self.assertEqual(cfg.translation.slang["개복치"], "玻璃心")
        self.assertEqual(cfg.translation.slang["하덱스"], "HADES")
        self.assertEqual(cfg.translation.slang["예난"], "Yenan")
        self.assertEqual(cfg.translation.slang["철구"], "Chulgu")

    def test_default_slang_contains_global_glossary_terms(self):
        self.assertEqual(cfg.translation.slang["마크"], "Minecraft")
        self.assertEqual(cfg.translation.slang["섭주"], "服主")
        self.assertEqual(cfg.translation.slang["섭쥬"], "服主")
        self.assertEqual(cfg.translation.slang["썹주"], "服主")
        self.assertEqual(cfg.translation.slang["SUBJU"], "服主")
        self.assertEqual(cfg.translation.slang["섭쥬방"], "服主房")

    def test_default_slang_removes_conflicting_bare_person_names(self):
        for key in ("챈나", "키마", "봉준", "김봉준", "성태", "히나"):
            self.assertNotIn(key, cfg.translation.slang)

        self.assertEqual(cfg.translation.slang["시라유키 히나"], "Shirayuki Hina")
        self.assertEqual(cfg.translation.slang["아야츠노 유니"], "Ayatsuno Yuni")

    def test_default_slang_glossary_values_are_direct_outputs(self):
        explanatory_fragments = ("遊戲", "人名", "마인크래프트", "Server Owner")
        for key in ("마크", "섭주", "섭쥬", "썹주", "SUBJU", "섭쥬방"):
            value = cfg.translation.slang[key]
            self.assertNotEqual(value.strip(), "")
            for fragment in explanatory_fragments:
                self.assertNotIn(fragment, value)

    def test_default_slang_is_immutable_mapping(self):
        with self.assertRaises(TypeError):
            cfg.translation.slang["test"] = "value"  # type: ignore[index]

    def test_translation_temperature_range(self):
        self.assertGreaterEqual(cfg.translation.temperature, 0.0)
        self.assertLessEqual(cfg.translation.temperature, 1.0)

    def test_subtitle_alpha_range(self):
        self.assertGreater(cfg.subtitle.alpha, 0.0)
        self.assertLessEqual(cfg.subtitle.alpha, 1.0)

    def test_subtitle_idle_hide_ms(self):
        self.assertGreater(cfg.subtitle.idle_hide_ms, 0)

    def test_frozen_raises_on_set(self):
        with self.assertRaises((TypeError, AttributeError)):
            cfg.audio.sample_rate = 999  # type: ignore

    def test_queue_sizes_positive(self):
        self.assertGreater(cfg.audio.queue_maxsize, 0)
        self.assertGreater(cfg.stt.queue_maxsize, 0)
        self.assertGreater(cfg.translation.queue_maxsize, 0)
        self.assertGreater(cfg.subtitle.queue_maxsize, 0)

    def test_stt_profile_glossary_flag_is_bool(self):
        self.assertIsInstance(cfg.stt.use_profile_glossary, bool)

    def test_stt_context_gate_defaults_are_stricter_than_reject_thresholds(self):
        self.assertGreater(cfg.stt.context_avg_logprob_threshold, cfg.stt.avg_logprob_threshold)
        self.assertLess(cfg.stt.context_no_speech_threshold, cfg.stt.no_speech_threshold)
        self.assertGreater(cfg.stt.context_max_age_sec, 0)
        self.assertGreater(cfg.stt.context_min_chars, 0)
        self.assertIsInstance(cfg.stt.dedupe_by_timestamp, bool)

    def test_groq_stt_fails_fast_for_live_subtitles(self):
        self.assertEqual(cfg.stt.groq_max_retries, 0)
        self.assertLessEqual(cfg.stt.groq_timeout, 10)
        self.assertGreaterEqual(cfg.stt.groq_rate_limit_cooldown_sec, 30)
        self.assertGreaterEqual(cfg.stt.groq_daily_request_limit, 2000)

    def test_nvidia_live_model_uses_benchmarked_fast_qwen(self):
        self.assertEqual(cfg.nvidia.model, "qwen/qwen3-next-80b-a3b-instruct")

    def test_nvidia_timeout_fails_fast_for_live_subtitles(self):
        self.assertLessEqual(cfg.nvidia.timeout, 10)

    def test_evolve_every_positive(self):
        self.assertGreater(cfg.translation.evolve_every, 0)


class TestVadConfig(unittest.TestCase):

    def test_vad_enabled_is_bool(self):
        self.assertIsInstance(cfg.audio.vad_enabled, bool)

    def test_vad_silence_sec_positive(self):
        self.assertGreater(cfg.audio.vad_silence_sec, 0)

    def test_vad_min_speech_sec_positive(self):
        self.assertGreater(cfg.audio.vad_min_speech_sec, 0)

    def test_vad_max_speech_greater_than_min(self):
        self.assertGreater(cfg.audio.vad_max_speech_sec, cfg.audio.vad_min_speech_sec)

    def test_vad_hard_max_not_less_than_soft_max(self):
        self.assertGreaterEqual(cfg.audio.vad_hard_max_speech_sec, cfg.audio.vad_max_speech_sec)

    def test_vad_overlap_is_shorter_than_soft_max(self):
        self.assertGreaterEqual(cfg.audio.vad_overlap_sec, 0)
        self.assertLess(cfg.audio.vad_overlap_sec, cfg.audio.vad_max_speech_sec)

    def test_vad_silence_overlap_is_shorter_than_soft_max(self):
        self.assertGreaterEqual(cfg.audio.vad_silence_overlap_sec, 0)
        self.assertLess(cfg.audio.vad_silence_overlap_sec, cfg.audio.vad_max_speech_sec)

    def test_vad_adaptive_limits_are_not_shorter_than_base(self):
        self.assertIsInstance(cfg.audio.vad_adaptive_enabled, bool)
        self.assertGreaterEqual(cfg.audio.vad_adaptive_after_boundary_cuts, 0)
        self.assertGreaterEqual(cfg.audio.vad_adaptive_silence_sec, cfg.audio.vad_silence_sec)
        self.assertGreaterEqual(cfg.audio.vad_adaptive_max_speech_sec, cfg.audio.vad_max_speech_sec)
        self.assertGreaterEqual(
            cfg.audio.vad_adaptive_hard_max_speech_sec,
            cfg.audio.vad_hard_max_speech_sec,
        )
        self.assertGreaterEqual(cfg.audio.vad_adaptive_overlap_sec, cfg.audio.vad_overlap_sec)

    def test_stt_normalization_config_is_bounded(self):
        self.assertIsInstance(cfg.audio.stt_normalize_enabled, bool)
        self.assertGreater(cfg.audio.stt_target_rms, 0)
        self.assertGreaterEqual(cfg.audio.stt_max_gain, 1)
        self.assertGreater(cfg.audio.stt_peak_limit, 0)
        self.assertLessEqual(cfg.audio.stt_peak_limit, 1)

    def test_vad_silence_less_than_max(self):
        # A silence gate larger than max_speech would mean we never cut on silence
        self.assertLess(cfg.audio.vad_silence_sec, cfg.audio.vad_max_speech_sec)

    def test_vad_min_less_than_max(self):
        self.assertLess(cfg.audio.vad_min_speech_sec, cfg.audio.vad_max_speech_sec)


if __name__ == "__main__":
    unittest.main()
