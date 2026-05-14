from unittest.mock import patch

from utils.metrics import PipelineMetrics


def test_metrics_counts_and_latency_summary():
    metrics = PipelineMetrics()

    metrics.increment("stt.success")
    metrics.increment("queue.text_queue.dropped", 2)
    metrics.observe_latency("stt", 0.1)
    metrics.observe_latency("stt", 0.3)

    snapshot = metrics.snapshot()

    assert snapshot.counters["stt.success"] == 1
    assert snapshot.counters["queue.text_queue.dropped"] == 2
    assert snapshot.latencies_ms["stt"] == 200
    assert "stt.avg_ms=200" in metrics.summary()


def test_metrics_ignores_non_positive_increment_and_negative_latency():
    metrics = PipelineMetrics()

    metrics.increment("ignored", 0)
    metrics.observe_latency("ignored", -1)

    snapshot = metrics.snapshot()

    assert snapshot.counters == {}
    assert snapshot.latencies_ms == {}


def test_metrics_summary_logs_only_when_due():
    metrics = PipelineMetrics(summary_interval_seconds=10)
    metrics._last_summary = 0

    with patch("utils.metrics.time.monotonic", side_effect=[5, 11]):
        assert metrics.log_summary_if_due() is False
        assert metrics.log_summary_if_due() is True


def test_metrics_reset_clears_state():
    metrics = PipelineMetrics()
    metrics.increment("translation.success")
    metrics.observe_latency("translation", 0.2)

    metrics.reset()

    snapshot = metrics.snapshot()
    assert snapshot.counters == {}
    assert snapshot.latencies_ms == {}
