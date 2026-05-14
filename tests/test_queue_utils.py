import queue
import unittest
from unittest.mock import MagicMock

from utils.queue_utils import drain_put, put_latest
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


if __name__ == "__main__":
    unittest.main()
