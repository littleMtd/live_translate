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
        self.assertGreater(cfg.audio.chunk_seconds, 0)
        self.assertGreater(cfg.audio.volume_threshold, 0)

    def test_splitter_ordering(self):
        # force cut 必須大於 min wait
        self.assertGreater(cfg.splitter.force_cut_seconds,
                           cfg.splitter.min_wait_seconds)

    def test_translation_engine_chain_valid(self):
        known = {"gemini", "claude", "google_translate", "deepseek", "deepl"}
        self.assertIsInstance(cfg.translation.engine_chain, tuple)
        self.assertGreater(len(cfg.translation.engine_chain), 0)
        for name in cfg.translation.engine_chain:
            self.assertIn(name, known)

    def test_translation_slang_is_dict(self):
        from collections.abc import Mapping
        self.assertIsInstance(cfg.translation.slang, Mapping)
        self.assertGreater(len(cfg.translation.slang), 0)

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

    def test_vad_silence_less_than_max(self):
        # A silence gate larger than max_speech would mean we never cut on silence
        self.assertLess(cfg.audio.vad_silence_sec, cfg.audio.vad_max_speech_sec)

    def test_vad_min_less_than_max(self):
        self.assertLess(cfg.audio.vad_min_speech_sec, cfg.audio.vad_max_speech_sec)


if __name__ == "__main__":
    unittest.main()
