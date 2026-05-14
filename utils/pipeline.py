import queue
import threading
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def start_daemon_thread(name: str, target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def wait_if_paused(
    stop_event: threading.Event,
    pause_event: threading.Event | None,
    timeout: float = 0.2,
) -> bool:
    if pause_event is None or not pause_event.is_set():
        return False
    stop_event.wait(timeout=timeout)
    return True


def wait_while_paused(
    stop_event: threading.Event,
    pause_event: threading.Event | None,
    timeout: float = 0.5,
) -> None:
    while pause_event is not None and pause_event.is_set() and not stop_event.is_set():
        stop_event.wait(timeout=timeout)


def poll_queue(
    input_queue: queue.Queue[T],
    stop_event: threading.Event,
    pause_event: threading.Event | None = None,
    timeout: float = 1.0,
) -> tuple[bool, T | None]:
    if wait_if_paused(stop_event, pause_event):
        return False, None
    try:
        return True, input_queue.get(timeout=timeout)
    except queue.Empty:
        return False, None
