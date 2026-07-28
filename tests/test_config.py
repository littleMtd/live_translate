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

    def test_live_fallback_chain_uses_benchmarked_openrouter_capsule_order(self):
        self.assertEqual(cfg.live_engine, "anthropic")
        self.assertEqual(cfg.clip_engine, "anthropic")
        self.assertEqual(
            cfg.translation.engine_chain,
            ("openrouter", "deepl", "groq"),
        )
        self.assertEqual(
            cfg.translation.openrouter_model,
            "qwen/qwen3-next-80b-a3b-instruct",
        )
        self.assertEqual(cfg.translation.openrouter_max_tokens, 160)
        self.assertTrue(cfg.translation.circuit_breaker_enabled)
        self.assertEqual(cfg.translation.circuit_recovery_success_threshold, 2)
        self.assertGreater(cfg.translation.live_total_deadline_sec, 0)
        self.assertEqual(cfg.translation.live_route_max_inflight, 2)
        self.assertEqual(cfg.translation.claude_timeout, 5.0)
        self.assertEqual(cfg.translation.google_translate_timeout, 5.0)

    def test_translation_reliability_limits_are_validated(self):
        from config import _Translation

        for field_name, value in (
            ("circuit_recovery_cooldown_sec", 0),
            ("circuit_recovery_cooldown_sec", float("inf")),
            ("live_total_deadline_sec", 0),
            ("live_total_deadline_sec", float("nan")),
        ):
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaisesRegex(ValueError, "positive and finite"):
                    _Translation(**{field_name: value})
        with self.assertRaisesRegex(ValueError, "at least 1"):
            _Translation(circuit_recovery_success_threshold=0)
        with self.assertRaisesRegex(ValueError, "must be boolean"):
            _Translation(circuit_breaker_enabled="yes")
        for value in (0, 9, True, 1.5):
            with self.subTest(live_route_max_inflight=value):
                with self.assertRaisesRegex(ValueError, "between 1 and 8"):
                    _Translation(live_route_max_inflight=value)

    def test_scene_vision_routes_are_explicit_groq_then_openrouter(self):
        self.assertEqual(cfg.scene.vision_provider, "groq")
        self.assertEqual(cfg.scene.vision_model, "qwen/qwen3.6-27b")
        self.assertEqual(
            cfg.scene.vision_fallback_routes,
            (("openrouter", "qwen/qwen3-vl-32b-instruct"),),
        )
        self.assertEqual(cfg.scene.vision_max_retries, 0)

    def test_scene_vision_rejects_unknown_malformed_and_duplicate_routes(self):
        from config import _Scene

        with self.assertRaisesRegex(ValueError, "provider invalid"):
            _Scene(vision_provider="implicit", vision_model="model")
        with self.assertRaisesRegex(ValueError, "malformed route"):
            _Scene(vision_fallback_routes=(("openrouter",),))
        with self.assertRaisesRegex(ValueError, "must be unique"):
            _Scene(
                vision_provider="groq",
                vision_model="same",
                vision_fallback_routes=(("groq", "same"),),
            )

    def test_scene_vision_rejects_blank_model_retry_and_invalid_timeout(self):
        from config import _Scene

        with self.assertRaisesRegex(ValueError, "model invalid"):
            _Scene(vision_model="")
        with self.assertRaisesRegex(ValueError, "model invalid"):
            _Scene(vision_model="model with unbounded instructions")
        with self.assertRaisesRegex(ValueError, "at most three"):
            _Scene(
                vision_fallback_routes=(
                    ("openrouter", "fallback-1"),
                    ("openrouter", "fallback-2"),
                    ("groq", "fallback-3"),
                )
            )
        with self.assertRaisesRegex(ValueError, "must remain zero"):
            _Scene(vision_max_retries=1)
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "must be positive"):
                    _Scene(vision_timeout=timeout)

    def test_scene_open_set_publication_is_default_off_and_bounded(self):
        from config import _Scene

        scene = _Scene()
        self.assertFalse(scene.publish_open_set_activity)
        self.assertGreaterEqual(scene.max_open_set_identities_per_window, 1)
        self.assertLessEqual(scene.max_open_set_identities_per_window, 32)

        for field_name in (
            "publish_translation_activity",
            "publish_open_set_activity",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(ValueError, "must be boolean"):
                    _Scene(**{field_name: "yes"})
        for value in (0, 33, True, 1.5):
            with self.subTest(max_open_set_identities_per_window=value):
                with self.assertRaisesRegex(ValueError, "between 1 and 32"):
                    _Scene(max_open_set_identities_per_window=value)

    def test_streamer_profile_ids_match_registry(self):
        from config import _VALID_STREAMER_PROFILES
        from modules.streamer_profiles import known_profile_ids

        self.assertEqual(_VALID_STREAMER_PROFILES, known_profile_ids(include_aliases=True))

    def test_active_streamer_profile_matches_translation_profile(self):
        self.assertEqual(cfg.active_streamer_profile, cfg.translation.streamer_profile)

    def test_streamer_profile_alias_is_accepted_and_canonicalized(self):
        from config import _Config, _Translation

        custom = _Config(translation=_Translation(streamer_profile="hades"))

        self.assertEqual(custom.translation.streamer_profile, "hades")
        self.assertEqual(custom.active_streamer_profile, "hades_chxxnnx")

    def test_streamer_profile_alias_validation_is_case_insensitive(self):
        from config import _Config, _Translation

        custom = _Config(translation=_Translation(streamer_profile="HADES"))

        self.assertEqual(custom.active_streamer_profile, "hades_chxxnnx")

    def test_unknown_streamer_profile_is_rejected(self):
        from config import _Config, _Translation

        with self.assertRaisesRegex(ValueError, "streamer_profile invalid"):
            _Config(translation=_Translation(streamer_profile="typo_profile"))

    def test_unknown_japanese_retry_mode_is_rejected(self):
        from config import _Translation

        with self.assertRaisesRegex(ValueError, "quality_retry_japanese_mode invalid"):
            _Translation(quality_retry_japanese_mode="replace-everything")

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

    def test_translation_queue_keeps_small_bursts(self):
        self.assertGreaterEqual(cfg.translation.queue_maxsize, 8)

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
        self.assertEqual(cfg.nvidia.live_timeout, 5)

    def test_legacy_nvidia_circuit_fields_remain_available_for_compatibility(self):
        self.assertTrue(cfg.nvidia.circuit_breaker_enabled)
        self.assertGreaterEqual(cfg.nvidia.recovery_cooldown_sec, 30.0)
        self.assertGreaterEqual(cfg.nvidia.recovery_success_threshold, 2)

    def test_translation_stale_subtitle_fuse_is_enabled(self):
        self.assertGreater(cfg.translation.max_subtitle_output_delay_ms, 0)
        self.assertLessEqual(cfg.translation.max_subtitle_output_delay_ms, 30000)


class TestVadConfig(unittest.TestCase):

    def test_vad_enabled_is_bool(self):
        self.assertIsInstance(cfg.audio.vad_enabled, bool)

    def test_vad_silence_sec_positive(self):
        self.assertGreater(cfg.audio.vad_silence_sec, 0)

    def test_vad_min_speech_sec_positive(self):
        self.assertGreater(cfg.audio.vad_min_speech_sec, 0)

    def test_vad_near_miss_threshold_keeps_short_speech_overlap(self):
        self.assertGreaterEqual(cfg.audio.vad_near_miss_min_speech_sec, 0)
        self.assertLess(cfg.audio.vad_near_miss_min_speech_sec, cfg.audio.vad_min_speech_sec)
        self.assertGreater(cfg.audio.vad_near_miss_overlap_sec, 0)
        self.assertLess(cfg.audio.vad_near_miss_overlap_sec, cfg.audio.vad_hard_max_speech_sec)

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

    def test_splitter_segment_boundary_flags_are_valid(self):
        self.assertIsInstance(cfg.splitter.segment_gap_split_enabled, bool)
        self.assertIsInstance(cfg.splitter.silence_complete_enabled, bool)
        self.assertGreater(cfg.splitter.segment_gap_seconds, 0)

    def test_vad_min_less_than_max(self):
        self.assertLess(cfg.audio.vad_min_speech_sec, cfg.audio.vad_max_speech_sec)


if __name__ == "__main__":
    unittest.main()
