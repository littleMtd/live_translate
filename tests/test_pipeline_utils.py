import queue
import threading
import unittest

from utils.pipeline import poll_queue, start_daemon_thread, wait_if_paused, wait_while_paused


class TestPipelineThreadUtils(unittest.TestCase):
    def test_start_daemon_thread_runs_target(self):
        done = threading.Event()

        thread = start_daemon_thread("TestPipelineThread", done.set)
        self.assertTrue(thread.daemon)
        self.assertEqual(thread.name, "TestPipelineThread")
        self.assertTrue(done.wait(timeout=1))

    def test_wait_if_paused_returns_false_when_not_paused(self):
        stop = threading.Event()
        pause = threading.Event()

        self.assertFalse(wait_if_paused(stop, pause, timeout=0.001))

    def test_wait_if_paused_returns_true_when_paused(self):
        stop = threading.Event()
        pause = threading.Event()
        pause.set()

        self.assertTrue(wait_if_paused(stop, pause, timeout=0.001))

    def test_wait_while_paused_exits_when_stop_set(self):
        stop = threading.Event()
        pause = threading.Event()
        pause.set()
        stop.set()

        wait_while_paused(stop, pause, timeout=0.001)
        self.assertTrue(stop.is_set())

    def test_poll_queue_returns_item_when_available(self):
        items: queue.Queue[str] = queue.Queue()
        stop = threading.Event()
        items.put("token")

        has_item, item = poll_queue(items, stop, timeout=0.001)

        self.assertTrue(has_item)
        self.assertEqual(item, "token")

    def test_poll_queue_returns_false_when_empty(self):
        items: queue.Queue[str] = queue.Queue()
        stop = threading.Event()

        has_item, item = poll_queue(items, stop, timeout=0.001)

        self.assertFalse(has_item)
        self.assertIsNone(item)

    def test_poll_queue_returns_false_when_paused(self):
        items: queue.Queue[str] = queue.Queue()
        stop = threading.Event()
        pause = threading.Event()
        pause.set()
        items.put("stale")

        has_item, item = poll_queue(items, stop, pause, timeout=0.001)

        self.assertFalse(has_item)
        self.assertIsNone(item)
        self.assertEqual(items.get_nowait(), "stale")


if __name__ == "__main__":
    unittest.main()
