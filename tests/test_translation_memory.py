import unittest
from collections import OrderedDict, deque
from unittest.mock import MagicMock

from modules.translation_memory import TranslationMemory
from utils.metrics import metrics


class _FakeDB:
    def __init__(self):
        self.lookup_result = None
        self.lookup_calls = []
        self.store_calls = []

    def lookup(self, *args):
        self.lookup_calls.append(args)
        return self.lookup_result

    def store(self, *args):
        self.store_calls.append(args)


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

    def test_lookup_existing_skips_db_for_incomplete(self):
        metrics.reset()
        memory, _, fake_db = self._memory()

        result = memory.lookup_existing("source", True, "v1", _engine())

        self.assertIsNone(result)
        self.assertEqual(fake_db.lookup_calls, [])
        self.assertEqual(metrics.snapshot().counters["translation.cache.miss"], 1)

    def test_lookup_existing_caches_db_hit(self):
        metrics.reset()
        fake_db = _FakeDB()
        fake_db.lookup_result = "db result"
        memory, _, _ = self._memory(fake_db)

        result = memory.lookup_existing("source", False, "v1", _engine())

        self.assertEqual(result, "db result")
        self.assertEqual(memory.cache_lookup("source", False, "v1"), "db result")
        self.assertEqual(list(memory.recent), [("source", "db result")])
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


if __name__ == "__main__":
    unittest.main()
