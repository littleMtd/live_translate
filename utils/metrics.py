from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from statistics import fmean

from utils.logger import get_logger


log = get_logger("metrics")


@dataclass(frozen=True)
class MetricsSnapshot:
    counters: dict[str, int]
    latencies_ms: dict[str, float]


class PipelineMetrics:
    def __init__(self, summary_interval_seconds: float = 60.0, max_latency_samples: int = 500):
        self._summary_interval_seconds = summary_interval_seconds
        self._max_latency_samples = max_latency_samples
        self._counters: dict[str, int] = {}
        self._latencies_ms: dict[str, list[float]] = {}
        self._last_summary = time.monotonic()
        self._lock = threading.Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def observe_latency(self, name: str, elapsed_seconds: float) -> None:
        if elapsed_seconds < 0:
            return
        elapsed_ms = elapsed_seconds * 1000
        with self._lock:
            samples = self._latencies_ms.setdefault(name, [])
            samples.append(elapsed_ms)
            if len(samples) > self._max_latency_samples:
                del samples[:len(samples) - self._max_latency_samples]

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            counters = dict(self._counters)
            latencies_ms = {
                name: fmean(samples)
                for name, samples in self._latencies_ms.items()
                if samples
            }
        return MetricsSnapshot(counters=counters, latencies_ms=latencies_ms)

    def summary(self) -> str:
        snapshot = self.snapshot()
        parts = [
            f"{name}={value}"
            for name, value in sorted(snapshot.counters.items())
        ]
        parts.extend(
            f"{name}.avg_ms={value:.0f}"
            for name, value in sorted(snapshot.latencies_ms.items())
        )
        return " | ".join(parts) if parts else "no metrics yet"

    def log_summary_if_due(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last_summary < self._summary_interval_seconds:
                return False
            self._last_summary = now
        log.info("Pipeline summary | %s", self.summary())
        return True

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._latencies_ms.clear()
            self._last_summary = time.monotonic()


metrics = PipelineMetrics()
