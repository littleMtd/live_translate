import logging
import queue

from utils.metrics import metrics

log = logging.getLogger("queue_utils")


def drain_put(q: queue.Queue, item) -> int:
    """Put item into q, draining all stale entries first if full.

    Returns the number of items drained (0 = normal put, >0 = backlog cleared).
    Live-subtitle strategy: newest content always wins over completeness.
    """
    try:
        q.put_nowait(item)
        return 0
    except queue.Full:
        drained = 0
        try:
            while True:
                q.get_nowait()
                drained += 1
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            # Should never happen: we just drained the queue and this is the
            # only producer. If it does, it means another producer is racing —
            # a pipeline architecture bug.
            log.error(
                "drain_put: queue still full after drain — item dropped. "
                "Check for multiple concurrent producers on the same queue."
            )
        return drained


def put_latest(q: queue.Queue, item, logger, queue_name: str, unit: str = "items") -> int:
    """Put latest item and log when stale backlog is dropped."""
    drained = drain_put(q, item)
    if drained:
        metrics.increment(f"queue.{queue_name}.dropped", drained)
        logger.warning("%s backlog cleared (%d %s), keeping latest", queue_name, drained, unit)
    return drained
