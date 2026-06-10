import queue
import unittest
from unittest.mock import MagicMock

from utils.queue_utils import drain_put, drop_oldest_put, put_drop_oldest, put_latest
from utils.metrics import metrics


class TestQueueUtils(unittest.TestCase):
    def test_drain_put_adds_item_when_queue_has_space(self):
        items = queue.Queue(maxsize=2)

        drained = drain_put(items, "new")

        self.assertEqual(drained, 0)
        self.assertEqual(items.get_nowait(), "new")

    def test_drain_put_drops_stale_items_when_full(self):
        items = queue.Queue(maxsize=1)
        items.put_nowait("old")

        drained = drain_put(items, "new")

        self.assertEqual(drained, 1)
        self.assertEqual(items.get_nowait(), "new")

    def test_put_latest_logs_when_backlog_is_cleared(self):
        metrics.reset()
        items = queue.Queue(maxsize=1)
        items.put_nowait("old")
        logger = MagicMock()

        drained = put_latest(items, "new", logger, "test_queue", "tokens")

        self.assertEqual(drained, 1)
        self.assertEqual(metrics.snapshot().counters["queue.test_queue.dropped"], 1)
        logger.warning.assert_called_once_with(
            "%s backlog cleared (%d %s), keeping latest",
            "test_queue",
            1,
            "tokens",
        )

    def test_put_latest_does_not_log_when_no_backlog(self):
        items = queue.Queue(maxsize=1)
        logger = MagicMock()

        drained = put_latest(items, "new", logger, "test_queue")

        self.assertEqual(drained, 0)
        logger.warning.assert_not_called()

    def test_drop_oldest_put_keeps_existing_backlog_except_oldest(self):
        items = queue.Queue(maxsize=3)
        for value in ("oldest", "middle", "newer"):
            items.put_nowait(value)

        dropped = drop_oldest_put(items, "latest")

        self.assertEqual(dropped, 1)
        self.assertEqual([items.get_nowait() for _ in range(3)], ["middle", "newer", "latest"])

    def test_put_drop_oldest_logs_single_drop(self):
        metrics.reset()
        items = queue.Queue(maxsize=1)
        items.put_nowait("old")
        logger = MagicMock()

        dropped = put_drop_oldest(items, "new", logger, "sentence_queue", "items")

        self.assertEqual(dropped, 1)
        self.assertEqual(metrics.snapshot().counters["queue.sentence_queue.dropped"], 1)
        logger.warning.assert_called_once_with(
            "%s oldest item dropped (%d %s), keeping backlog",
            "sentence_queue",
            1,
            "items",
        )


if __name__ == "__main__":
    unittest.main()
