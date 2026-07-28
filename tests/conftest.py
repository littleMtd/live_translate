"""
pytest conftest — shared fixtures for the test suite.

Patches the modules.db singleton to an unavailable (no-op) instance
for all tests that don't explicitly inject their own DB. This prevents
any real on-disk DB from influencing unit tests that test API/cache
logic in isolation.

Individual DB integration tests override this by setting
``modules.db._db`` (and ``modules.translator`` imports it via
``from modules.db import _get_db``) inside their own setUp/tearDown.
"""

import os
from pathlib import Path
import sys

# Keep pytest-owned cache/temp artifacts out of the repository. This host's
# standard Windows pytest temp root has also been observed with broken ACLs, so
# use a dedicated user-owned root while retaining pytest's numbered per-run
# directories and retention behavior. Respect an explicit caller override.
_PYTEST_TEMP_ROOT = Path.home() / ".cache" / "live_translate" / "pytest" / "tmp"
if "PYTEST_DEBUG_TEMPROOT" not in os.environ:
    _PYTEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_PYTEST_TEMP_ROOT)

# Stub out API libraries before any module imports them.
from unittest.mock import MagicMock

for _mod in ("anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest
import modules.db as _db_module


class _NoOpDB:
    """Mimics TranslationDB interface but does nothing — for isolation."""

    @property
    def available(self) -> bool:
        return False

    def lookup(self, *args, **kwargs):
        return None

    def store(self, *args, **kwargs):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _isolate_db():
    """Replace the DB singleton with a no-op instance for every test.

    Tests inside test_db.py manage their own DB by overwriting
    ``modules.db._db`` themselves in setUp/tearDown.
    """
    original = _db_module._db
    _db_module._db = _NoOpDB()
    try:
        yield
    finally:
        _db_module._db = original


@pytest.fixture(autouse=True)
def _isolate_runtime_logs(tmp_path):
    """Redirect the runtime_events singleton and translation history writes
    to tmp_path for every test.

    Without this, test runs append mock translation/stt events into
    ``logs/runtime_events_YYYYMMDD.jsonl`` and ``logs/translations_*.txt``,
    polluting the production logs that quality scans read (observed: 72+
    mock-engine events across 20260624-20260705). Tests that need a real
    writer construct their own ``RuntimeEventWriter(log_dir=tmp_path)``
    and are unaffected.
    """
    from utils.runtime_events import runtime_events
    import modules.translator as _translator_module

    original_events_dir = runtime_events._log_dir
    original_history_dir = _translator_module._LOG_DIR
    runtime_events._log_dir = tmp_path
    _translator_module._LOG_DIR = tmp_path
    try:
        yield
    finally:
        runtime_events._log_dir = original_events_dir
        _translator_module._LOG_DIR = original_history_dir
