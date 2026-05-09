import sys

# Stub missing packages so tests run without venv
from unittest.mock import MagicMock
if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = MagicMock()

import json
import unittest
from unittest.mock import patch

from modules.prompt_evolver import PromptEvolver


class TestBuildSystemPrompt(unittest.TestCase):

    def setUp(self):
        self.evolver = PromptEvolver()
        self.base = "BASE_PROMPT"

    def test_returns_base_when_no_evolution(self):
        self.assertEqual(self.evolver.build_system_prompt(self.base), self.base)

    def test_appends_stream_context(self):
        self.evolver._stream_context = "Minecraft 遊戲直播"
        result = self.evolver.build_system_prompt(self.base)
        self.assertIn("Minecraft 遊戲直播", result)
        self.assertIn(self.base, result)

    def test_appends_extra_slang(self):
        self.evolver._extra_slang = {"존버": "撐著"}
        result = self.evolver.build_system_prompt(self.base)
        self.assertIn("존버→撐著", result)

    def test_appends_both_context_and_slang(self):
        self.evolver._stream_context = "FPS 遊戲"
        self.evolver._extra_slang = {"gg": "GG"}
        result = self.evolver.build_system_prompt(self.base)
        self.assertIn("FPS 遊戲", result)
        self.assertIn("gg→GG", result)


class TestRecord(unittest.TestCase):

    def setUp(self):
        self.evolver = PromptEvolver()

    def _record_enabled(self, ko, zh):
        """Call record() with evolve_enabled=True regardless of config default."""
        with patch("modules.prompt_evolver.cfg") as mock_cfg:
            mock_cfg.translation.evolve_enabled = True
            mock_cfg.translation.evolve_every = 20
            self.evolver.record(ko, zh)

    def test_record_increments_count(self):
        self._record_enabled("안녕", "你好")
        self.assertEqual(self.evolver._count, 1)

    def test_record_adds_to_buffer(self):
        self._record_enabled("안녕", "你好")
        self.assertEqual(len(self.evolver._buffer), 1)
        self.assertEqual(self.evolver._buffer[0], ("안녕", "你好"))

    def test_buffer_respects_maxlen(self):
        maxlen = self.evolver._buffer.maxlen
        for i in range(maxlen + 10):
            self._record_enabled(f"ko{i}", f"zh{i}")
        self.assertEqual(len(self.evolver._buffer), maxlen)

    def test_no_record_when_disabled(self):
        with patch("modules.prompt_evolver.cfg") as mock_cfg:
            mock_cfg.translation.evolve_enabled = False
            self.evolver.record("안녕", "你好")
        self.assertEqual(self.evolver._count, 0)


class TestAnalyze(unittest.TestCase):

    def _make_evolver_with_data(self) -> PromptEvolver:
        e = PromptEvolver()
        e._buffer.extend([("ko", "zh")] * 5)
        return e

    def test_analyze_updates_slang_and_context(self):
        evolver = self._make_evolver_with_data()
        payload = json.dumps({"new_slang": {"존버": "硬撐"}, "stream_context": "Minecraft", "corrections": []})
        with patch.object(evolver, "_call_gemini", return_value=payload):
            evolver._analyze()

        self.assertEqual(evolver._extra_slang.get("존버"), "硬撐")
        self.assertEqual(evolver._stream_context, "Minecraft")
        self.assertFalse(evolver._analyzing)

    def test_analyze_handles_invalid_json(self):
        evolver = self._make_evolver_with_data()
        with patch.object(evolver, "_call_gemini", return_value="not-valid-json"):
            evolver._analyze()

        self.assertFalse(evolver._analyzing)
        self.assertEqual(evolver._extra_slang, {})

    def test_analyzing_flag_cleared_on_exception(self):
        evolver = self._make_evolver_with_data()
        with patch.object(evolver, "_call_gemini", side_effect=RuntimeError("boom")):
            evolver._analyze()

        self.assertFalse(evolver._analyzing)


if __name__ == "__main__":
    unittest.main()
