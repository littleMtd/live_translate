import unittest
from collections import deque

from modules.translation_memory import TranslationMemory


class TestTranslationMemory(unittest.TestCase):
    def _memory(self) -> tuple[TranslationMemory, list[tuple[str, str]]]:
        history: list[tuple[str, str]] = []
        memory = TranslationMemory(
            recent=deque(maxlen=3),
            history_writer=lambda source, result: history.append((source, result)),
        )
        return memory, history

    def test_recent_history_is_isolated_by_session_activity_and_episode(self):
        memory, _ = self._memory()
        chat_a = ("session-a", "chatting", 1)
        lol_a = ("session-a", "league_of_legends", 2)
        chat_b = ("session-b", "chatting", 1)
        chat_return = ("session-a", "chatting", 3)

        memory.record_recent_context("chat", "聊天", False, chat_a)
        memory.record_recent_context("lol", "遊戲", False, lol_a)
        memory.record_recent_context("other", "另一人", False, chat_b)

        self.assertEqual(memory.context(chat_a), [("chat", "聊天")])
        self.assertEqual(memory.context(lol_a), [("lol", "遊戲")])
        self.assertEqual(memory.context(chat_b), [("other", "另一人")])
        self.assertEqual(memory.context(chat_return), [])
        self.assertEqual(memory.context(("session-a", "unknown", 4)), [])

    def test_recent_cohort_map_is_lru_bounded(self):
        memory = TranslationMemory(
            recent_window=2,
            max_recent_cohorts=2,
            history_writer=lambda _source, _result: None,
        )
        for epoch in range(1, 4):
            memory.record_recent_context(
                f"source-{epoch}", f"翻譯-{epoch}", False,
                ("profile", "unknown", epoch),
            )

        self.assertEqual(memory.context(("profile", "unknown", 1)), [])
        self.assertEqual(len(memory._recent_by_cohort), 2)

    def test_recent_replaces_existing_source_instead_of_appending_duplicate(self):
        memory, _ = self._memory()

        memory.record_direct("source", "first", False)
        memory.record_direct("source", "second", False)

        self.assertEqual(list(memory.recent), [("source", "second")])

    def test_record_direct_writes_the_history_file(self):
        memory, history = self._memory()

        memory.record_direct("source", "result", False)
        memory.write_history("other", "另一句")

        self.assertEqual(history, [("source", "result"), ("other", "另一句")])


if __name__ == "__main__":
    unittest.main()
