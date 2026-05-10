import io
import logging
import os
import sys

_LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

# One handler on the root logger shared by all child loggers.
# Per-logger handlers sharing the same stream would each hold a different lock,
# allowing concurrent writes to interleave on Windows CJK consoles.
_UTF8_STREAM = (
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer") else sys.stdout
)

_root_configured = False


def _configure_root() -> None:
    global _root_configured
    if _root_configured:
        return
    _root_configured = True
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) and h.stream is _UTF8_STREAM
               for h in root.handlers):
        handler = logging.StreamHandler(_UTF8_STREAM)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        ))
        root.addHandler(handler)
    root.setLevel(_LOG_LEVEL)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    logger = logging.getLogger(name)
    logger.propagate = True
    return logger
