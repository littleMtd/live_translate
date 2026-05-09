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

import sys

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
