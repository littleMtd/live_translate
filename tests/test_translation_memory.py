import unittest
from collections import OrderedDict, deque
from unittest.mock import MagicMock

from modules.translation_memory import MemoryLookup, TranslationMemory
from utils.metrics import metrics


class _FakeDB:
    def __init__(self):
        self.lookup_result = None
        self.lookup_calls = []
        self.store_calls = []
        self.delete_calls = []

    def lookup(self, *args):
        self.lookup_calls.append(args)
        return self.lookup_result

    def store(self, *args):
        self.store_calls.append(args)

    def delete(self, *args):
        self.delete_calls.append(args)


def _engine() -> MagicMock:
    engine = MagicMock()
    engine.engine_name = "engine"
    engine.model_name = "model"
    return engine


class TestTranslationMemory(unittest.TestCase):
    def _memory(self, db: _FakeDB | None = None) -> tuple[TranslationMemory, list[tuple[str, str]], _FakeDB]:
        fake_db = db or _FakeDB()
        history: list[tuple[str, str]] = []
        memory = TranslationMemory(
            cache=OrderedDict(),
            recent=deque(maxlen=3),
            max_cache_size=2,
            db_factory=lambda: fake_db,
            history_writer=lambda source, result: history.append((source, result)),
        )
        return memory, history, fake_db

    def test_lookup_existing_uses_cache_before_db(self):
        metrics.reset()
        memory, _, fake_db = self._memory()
        memory.cache_store("source", False, "cached", "v1")

        result = memory.lookup_existing("source", False, "v1", _engine())

        self.assertEqual(result, "cached")
        self.assertEqual(fake_db.lookup_calls, [])
        self.assertEqual(list(memory.recent), [("source", "cached")])
        self.assertEqual(metrics.snapshot().counters["translation.cache.memory_hit"], 1)

    def test_lookup_existing_event_reports_source(self):
        metrics.reset()
        memory, _, _ = self._memory()
        memory.cache_store("source", False, "cached", "v1")

        result = memory.lookup_existing_event("source", False, "v1", _engine())

        self.assertEqual(result, MemoryLookup("cached", "memory_hit"))

    def test_lookup_existing_skips_db_for_incomplete(self):
        metrics.reset()
        memory, _, fake_db = self._memory()

        result = memory.lookup_existing("source", True, "v1", _engine())

        self.assertIsNone(result)
        self.assertEqual(fake_db.lookup_calls, [])
        self.assertEqual(metrics.snapshot().counters["translation.cache.miss"], 1)
        self.assertEqual(
            memory.lookup_existing_event("source", True, "v1", _engine()).source,
            "skipped",
        )

    def test_lookup_existing_caches_db_hit(self):
        metrics.reset()
        fake_db = _FakeDB()
        fake_db.lookup_result = "資料庫結果"
        memory, _, _ = self._memory(fake_db)

        result = memory.lookup_existing("source", False, "v1", _engine())

        self.assertEqual(result, "資料庫結果")
        self.assertEqual(memory.cache_lookup("source", False, "v1"), "資料庫結果")
        self.assertEqual(list(memory.recent), [("source", "資料庫結果")])
        self.assertEqual(metrics.snapshot().counters["translation.cache.db_hit"], 1)

    def test_record_success_writes_complete_translation_to_db(self):
        memory, history, fake_db = self._memory()
        engine = _engine()

        memory.record_success("source", "result", False, "v1", engine)

        self.assertEqual(history, [("source", "result")])
        self.assertEqual(list(memory.recent), [("source", "result")])
        self.assertEqual(memory.cache_lookup("source", False, "v1"), "result")
        self.assertEqual(len(fake_db.store_calls), 1)

    def test_record_success_skips_db_for_incomplete(self):
        memory, history, fake_db = self._memory()

        memory.record_success("source", "result", True, "v1", _engine())

        self.assertEqual(history, [("source", "result")])
        self.assertEqual(list(memory.recent), [])
        self.assertEqual(fake_db.store_calls, [])

    def test_record_success_gates_bad_quality_from_recent_but_keeps_cache_db_history(self):
        metrics.reset()
        memory, history, fake_db = self._memory()
        engine = _engine()
        source = "안녕하세요 여러분 오늘 방송입니다"
        bad_target = "I cannot translate this STT garbage"

        memory.record_success(source, bad_target, False, "v1", engine)

        self.assertEqual(history, [(source, bad_target)])
        self.assertEqual(memory.cache_lookup(source, False, "v1"), bad_target)
        self.assertEqual(list(memory.recent), [])
        self.assertEqual(len(fake_db.store_calls), 1)
        counters = metrics.snapshot().counters
        self.assertEqual(counters["translation.context_gated"], 1)
        self.assertEqual(counters["translation.context_gated.bad"], 1)

    def test_memory_hit_bad_quality_does_not_enter_recent(self):
        metrics.reset()
        memory, _, _ = self._memory()
        source = "안녕하세요 여러분 오늘 방송입니다"
        bad_target = "I cannot translate this STT garbage"
        memory.cache_store(source, False, bad_target, "v1")

        result = memory.lookup_existing(source, False, "v1", _engine())

        self.assertEqual(result, bad_target)
        self.assertEqual(list(memory.recent), [])
        counters = metrics.snapshot().counters
        self.assertEqual(counters["translation.cache.memory_hit"], 1)
        self.assertEqual(counters["translation.context_gated"], 1)

    def test_db_hit_bad_quality_is_cached_but_not_remembered_recent(self):
        metrics.reset()
        fake_db = _FakeDB()
        source = "안녕하세요 여러분 오늘 방송입니다"
        bad_target = "I cannot translate this STT garbage"
        fake_db.lookup_result = bad_target
        memory, _, _ = self._memory(fake_db)

        result = memory.lookup_existing(source, False, "v1", _engine())

        self.assertEqual(result, bad_target)
        self.assertEqual(memory.cache_lookup(source, False, "v1"), bad_target)
        self.assertEqual(list(memory.recent), [])
        counters = metrics.snapshot().counters
        self.assertEqual(counters["translation.cache.db_hit"], 1)
        self.assertEqual(counters["translation.context_gated"], 1)

    def test_invalidate_removes_cache_recent_and_db_entry(self):
        memory, _, fake_db = self._memory()
        engine = _engine()
        memory.cache_store("source", False, "poisoned", "v1")
        memory.recent.append(("source", "poisoned"))
        memory.recent.append(("other", "safe"))

        memory.invalidate("source", False, "v1", engine, "poisoned")

        self.assertIsNone(memory.cache_lookup("source", False, "v1"))
        self.assertEqual(list(memory.recent), [("other", "safe")])
        self.assertEqual(
            fake_db.delete_calls,
            [("source", "zh-TW", "engine", "model", "v1")],
        )

    def test_invalidate_incomplete_skips_db_delete(self):
        memory, _, fake_db = self._memory()
        memory.cache_store("source", True, "poisoned", "v1")

        memory.invalidate("source", True, "v1", _engine(), "poisoned")

        self.assertIsNone(memory.cache_lookup("source", True, "v1"))
        self.assertEqual(fake_db.delete_calls, [])


if __name__ == "__main__":
    unittest.main()
