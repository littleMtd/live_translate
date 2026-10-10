import io
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

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



_file_handler: logging.Handler | None = None
_APP_LOG_RETENTION_DAYS = 30
# ElevenLabs ApiError.__str__ embeds the provider response headers (cookies etc.)
# and body; keep neither in a persisted file.
_HEADERS_RE = re.compile(r"headers:\s*\{.*?\}(?=,\s*status_code|,\s*body|$)", re.DOTALL)
_BODY_RE = re.compile(r"body:\s*.*$", re.DOTALL)


class _RedactingFormatter(logging.Formatter):
    """File formatter: drop provider response headers/bodies and configured secrets."""

    def __init__(self, fmt: str, datefmt: str, secrets: tuple[str, ...]):
        super().__init__(fmt, datefmt=datefmt)
        self._secrets = tuple(sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        text = _HEADERS_RE.sub("headers: <redacted>", text)
        text = _BODY_RE.sub("body: <redacted>", text)
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text


class _DailyFileHandler(logging.FileHandler):
    """Append to <dir>/app_YYYYMMDD.log, switching files when the local date changes."""

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._day = self._today()
        # Open now (not delayed) so enable_file_logging's try/except sees open failures.
        super().__init__(self._path(self._day), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y%m%d")

    def _path(self, day: str) -> Path:
        return self._log_dir / f"app_{day}.log"

    def emit(self, record: logging.LogRecord) -> None:
        # FileHandler opens the new day's file outside StreamHandler's error
        # handling; a failure there must never propagate into a pipeline thread.
        try:
            day = self._today()
            if day != self._day:
                self.acquire()
                try:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
                    self._day = day
                    self.baseFilename = str(self._path(day).resolve())
                finally:
                    self.release()
            super().emit(record)
        except Exception:
            self.handleError(record)


def _prune_old_app_logs(log_dir: Path, keep_days: int) -> None:
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y%m%d")
    for path in log_dir.glob("app_????????.log"):
        if path.stem[4:] < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def enable_file_logging(log_dir, run_id: str = "", secrets: tuple[str, ...] = ()) -> str | None:
    """Mirror console log lines into <log_dir>/app_YYYYMMDD.log (local date, 30-day retention).

    Called by main.py only (never under pytest), so tests and helper scripts do
    not write log files. Provider error messages otherwise vanish with the
    console. Response headers/bodies and the given secrets are redacted.
    Returns the file path, or None when the directory cannot be prepared.
    """
    global _file_handler
    _configure_root()
    if _file_handler is not None:
        return getattr(_file_handler, "baseFilename", None)
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _prune_old_app_logs(log_dir, _APP_LOG_RETENTION_DAYS)
        handler = _DailyFileHandler(log_dir)
    except OSError as exc:
        logging.getLogger("logger").warning("App log file disabled: %s", type(exc).__name__)
        return None
    handler.setFormatter(_RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S", tuple(secrets)
    ))
    logging.getLogger().addHandler(handler)
    _file_handler = handler
    logging.getLogger("logger").info("=== run %s started; log file %s ===", run_id or "-", handler.baseFilename)
    return handler.baseFilename
