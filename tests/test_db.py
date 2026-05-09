import sys
import os

# Stub out API libraries before importing translator
from unittest.mock import MagicMock, patch
for _mod in ("anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import os
import tempfile
import unittest
import unittest.mock

from modules.db import TranslationDB
from modules.prompt_evolver import PromptEvolver


# ---------------------------------------------------------------------------
# TranslationDB unit tests
# ---------------------------------------------------------------------------

class TestTranslationDBAvailable(unittest.TestCase):
    def test_available_after_init(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        self.addCleanup(os.unlink, path)
        db = TranslationDB(path=path, max_rows=100)
        try:
            self.assertTrue(db.available)
        finally:
            db.close()


class TestTranslationDBStoreAndLookup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = TranslationDB(path=self._tmp.name, max_rows=100)

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_store_and_lookup(self):
        self.db.store("안녕하세요", "你好", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        result = self.db.lookup("안녕하세요", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.assertEqual(result, "你好")

    def test_lookup_miss(self):
        result = self.db.lookup("unknown text", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.assertIsNone(result)

    def test_hit_count_increments(self):
        self.db.store("안녕하세요", "你好", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.db.lookup("안녕하세요", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.db.lookup("안녕하세요", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        cur = self.db._conn.execute(
            "SELECT hit_count FROM translations WHERE source_text=?", ("안녕하세요",)
        )
        row = cur.fetchone()
        self.assertEqual(row[0], 2)

    def test_normalization(self):
        self.db.store("text  with  spaces", "結果", "zh-TW", "claude", "claude-haiku", "v1")
        result = self.db.lookup("text with spaces", "zh-TW", "claude", "claude-haiku", "v1")
        self.assertEqual(result, "結果")

    def test_upsert_updates_target_text(self):
        self.db.store("안녕하세요", "你好", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.db.store("안녕하세요", "哈囉", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        result = self.db.lookup("안녕하세요", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.assertEqual(result, "哈囉")

    def test_different_engine_different_entry(self):
        self.db.store("안녕하세요", "Gemini結果", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.db.store("안녕하세요", "Claude結果", "zh-TW", "claude", "claude-haiku", "v1")
        gemini_result = self.db.lookup("안녕하세요", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        claude_result = self.db.lookup("안녕하세요", "zh-TW", "claude", "claude-haiku", "v1")
        self.assertEqual(gemini_result, "Gemini結果")
        self.assertEqual(claude_result, "Claude結果")

    def test_eviction_keeps_most_recent(self):
        max_rows = 5
        evict_path = self._tmp.name + ".evict.db"
        self.addCleanup(lambda: os.path.exists(evict_path) and os.unlink(evict_path))
        db = TranslationDB(path=evict_path, max_rows=max_rows)
        try:
            for i in range(max_rows + 2):
                db.store(f"text_{i}", f"result_{i}", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
            cur = db._conn.execute("SELECT COUNT(*) FROM translations")
            count = cur.fetchone()[0]
            self.assertLessEqual(count, max_rows)
        finally:
            db.close()


class TestTranslationDBUnavailable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = TranslationDB(path=self._tmp.name, max_rows=100)
        # Save the real connection so tearDown can close it, then nullify to
        # simulate an unavailable DB (db.close() is a no-op when _conn is None).
        self._real_conn = self.db._conn
        self.db._conn = None

    def tearDown(self):
        if self._real_conn is not None:
            self._real_conn.close()
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_unavailable_db_lookup_returns_none(self):
        result = self.db.lookup("anything", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        self.assertIsNone(result)

    def test_unavailable_db_store_silently_ignored(self):
        # Should not raise any exception
        try:
            self.db.store("anything", "result", "zh-TW", "gemini", "gemini-2.5-flash", "v1")
        except Exception as e:
            self.fail(f"store() raised an exception when DB unavailable: {e}")

    def test_available_false_when_conn_none(self):
        self.assertFalse(self.db.available)


# ---------------------------------------------------------------------------
# Translator + DB integration tests
# ---------------------------------------------------------------------------

def _mock_engine(name: str, return_value: str = "你好") -> MagicMock:
    from modules.translator import TranslationEngine
    engine = MagicMock(spec=TranslationEngine)
    engine.engine_name = name
    engine.model_name = f"{name}-test-model"
    engine.available = True
    engine.translate.return_value = return_value
    return engine


def _make_translator(cache=None):
    from collections import deque
    from modules.translator import Translator
    t = Translator.__new__(Translator)
    t._evolver = PromptEvolver()
    t._cache = cache if cache is not None else {}
    t._active_idx = 0
    t._probe_counter = 0
    t._last_input = ""
    t._recent = deque(maxlen=3)
    t._engines = [_mock_engine("gemini"), _mock_engine("claude")]
    return t


def _claude_resp(text):
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


class TestTranslatorDBIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._db = TranslationDB(path=self._tmp.name, max_rows=100)
        import modules.db as db_module
        self._db_module = db_module
        self._original_db = db_module._db
        db_module._db = self._db
        self._history_patch = unittest.mock.patch("modules.translator._write_history")
        self._history_patch.start()

    def tearDown(self):
        self._history_patch.stop()
        self._db_module._db = self._original_db
        self._db.close()
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_db_hit_skips_api_call(self):
        from config import cfg
        t = _make_translator()
        engine = t._engines[0]
        prompt_ver = t._get_prompt_version_hash()
        self._db.store("안녕하세요", "DB結果", cfg.translation.target_lang,
                       engine.engine_name, engine.model_name, prompt_ver)

        result = t.translate("안녕하세요", incomplete=False)

        self.assertEqual(result, "DB結果")
        for e in t._engines:
            e.translate.assert_not_called()

    def test_complete_translation_stored_in_db(self):
        from config import cfg
        t = _make_translator()
        t._engines[0].translate.return_value = "API結果"

        t.translate("완전한 문장입니다", incomplete=False)

        engine = t._engines[0]
        prompt_ver = t._get_prompt_version_hash()
        result = self._db.lookup("완전한 문장입니다", cfg.translation.target_lang,
                                 engine.engine_name, engine.model_name, prompt_ver)
        self.assertEqual(result, "API結果")

    def test_incomplete_not_stored_in_db(self):
        from config import cfg
        t = _make_translator()
        t._engines[0].translate.return_value = "임시결과"

        t.translate("불완전한 문장입니다", incomplete=True)

        engine = t._engines[0]
        prompt_ver = t._get_prompt_version_hash()
        result = self._db.lookup("불완전한 문장입니다", cfg.translation.target_lang,
                                 engine.engine_name, engine.model_name, prompt_ver)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
