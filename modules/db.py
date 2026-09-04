import re
import sqlite3
import os
import threading
import unicodedata
from datetime import datetime, timezone

from config import cfg
from utils.logger import get_logger

log = get_logger("db")

_SCHEMA_VERSION = "2"  # Incremented when UNIQUE constraint changed to include prompt_version

_DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text    TEXT    NOT NULL,
    target_text    TEXT    NOT NULL,
    source_lang    TEXT    NOT NULL DEFAULT 'ko',
    target_lang    TEXT    NOT NULL,
    engine         TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    hit_count      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    last_used_at   TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL DEFAULT 'v1',
    UNIQUE (source_text, target_lang, engine, model, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_last_used ON translations(last_used_at);
"""

_MIGRATE_v1_to_v2_STEPS = [
    # Cleanup guard: remove any leftover renamed table from a previously interrupted migration
    "DROP TABLE IF EXISTS translations_v1",
    "ALTER TABLE translations RENAME TO translations_v1",
    """CREATE TABLE translations (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source_text    TEXT    NOT NULL,
        target_text    TEXT    NOT NULL,
        source_lang    TEXT    NOT NULL DEFAULT 'ko',
        target_lang    TEXT    NOT NULL,
        engine         TEXT    NOT NULL,
        model          TEXT    NOT NULL,
        hit_count      INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT    NOT NULL,
        last_used_at   TEXT    NOT NULL,
        prompt_version TEXT    NOT NULL DEFAULT 'v1',
        UNIQUE (source_text, target_lang, engine, model, prompt_version)
    )""",
    """INSERT INTO translations
           (id, source_text, target_text, source_lang, target_lang, engine, model,
            hit_count, created_at, last_used_at, prompt_version)
       SELECT id, source_text, target_text, source_lang, target_lang, engine, model,
              hit_count, created_at, last_used_at, 'v1'
       FROM translations_v1""",
    "DROP TABLE translations_v1",
    "CREATE INDEX IF NOT EXISTS idx_last_used ON translations(last_used_at)",
]


def _normalize(text: str) -> str:
    """NFC-normalize then trim and collapse internal whitespace."""
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TranslationDB:
    def __init__(self, path: str | None = None, max_rows: int | None = None):
        self._path = path if path is not None else cfg.database.db_path
        self._max_rows = max_rows if max_rows is not None else cfg.database.db_cache_max_rows
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        try:
            # Ensure parent directory exists
            db_path = os.path.abspath(self._path)
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.executescript(_DDL)
            
            # Check current schema version
            cur = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )
            row = cur.fetchone()
            current_version = row[0] if row else None
            
            # Perform migrations if needed
            if current_version == "1":
                log.info("Migrating schema from v1 to v2...")
                self._conn.execute("BEGIN")
                try:
                    for step in _MIGRATE_v1_to_v2_STEPS:
                        self._conn.execute(step)
                    self._conn.execute(
                        "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                        (_SCHEMA_VERSION,),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                log.info("Schema migration completed")
            elif current_version is None:
                # First time setup
                self._conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                    ("schema_version", _SCHEMA_VERSION),
                )
                self._conn.commit()
            elif current_version != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported translation DB schema version {current_version!r}; "
                    f"expected {_SCHEMA_VERSION!r}"
                )
            
            log.info("TranslationDB ready: %s (schema_version=%s)", db_path, _SCHEMA_VERSION)
        except Exception as e:
            log.error("TranslationDB init failed: %s", e)
            if self._conn is not None:
                self._conn.close()
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def lookup(self, source_text: str, target_lang: str, engine: str, model: str, prompt_version: str = "v1") -> str | None:
        if self._conn is None:
            return None
        key = _normalize(source_text)
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT id, target_text FROM translations "
                    "WHERE source_text=? AND target_lang=? AND engine=? AND model=? AND prompt_version=?",
                    (key, target_lang, engine, model, prompt_version),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                row_id, target_text = row
                now = _now_iso()
                self._conn.execute(
                    "UPDATE translations SET hit_count = hit_count + 1, last_used_at=? WHERE id=?",
                    (now, row_id),
                )
                self._conn.commit()
            log.debug("DB cache hit: %s (prompt_version=%s)", key[:30], prompt_version)
            return target_text
        except Exception as e:
            log.error("DB lookup error: %s", e)
            return None

    def store(
        self,
        source_text: str,
        target_text: str,
        target_lang: str,
        engine: str,
        model: str,
        prompt_version: str = "v1",
    ) -> None:
        if self._conn is None:
            return
        key = _normalize(source_text)
        now = _now_iso()
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO translations
                       (source_text, target_text, source_lang, target_lang, engine, model,
                        hit_count, created_at, last_used_at, prompt_version)
                       VALUES (?, ?, 'ko', ?, ?, ?, 0, ?, ?, ?)
                       ON CONFLICT(source_text, target_lang, engine, model, prompt_version)
                       DO UPDATE SET
                           target_text    = excluded.target_text,
                           last_used_at   = excluded.last_used_at
                    """,
                    (key, target_text, target_lang, engine, model, now, now, prompt_version),
                )
                self._conn.commit()
            # Eviction uses its own short lock after the upsert commit.
            self._evict_if_needed()
        except Exception as e:
            log.error("DB store error: %s", e)

    def delete(
        self,
        source_text: str,
        target_lang: str,
        engine: str,
        model: str,
        prompt_version: str = "v1",
    ) -> None:
        if self._conn is None:
            return
        key = _normalize(source_text)
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM translations "
                    "WHERE source_text=? AND target_lang=? AND engine=? AND model=? AND prompt_version=?",
                    (key, target_lang, engine, model, prompt_version),
                )
                self._conn.commit()
            if cur.rowcount:
                log.debug("DB cache entry deleted: %s (prompt_version=%s)", key[:30], prompt_version)
        except Exception as e:
            log.error("DB delete error: %s", e)

    def _evict_if_needed(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                cur = self._conn.execute("SELECT COUNT(*) FROM translations")
                count = cur.fetchone()[0]
                if count <= self._max_rows:
                    return
                excess = count - self._max_rows
                self._conn.execute(
                    """DELETE FROM translations WHERE id IN (
                        SELECT id FROM translations ORDER BY last_used_at ASC LIMIT ?
                    )""",
                    (excess,),
                )
                self._conn.commit()
                log.debug("DB evicted %d rows (count was %d)", excess, count)
        except Exception as e:
            log.error("DB eviction error: %s", e)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as e:
                log.error("DB close error: %s", e)
            finally:
                self._conn = None


# Module-level singleton
_db: TranslationDB | None = None
_db_lock = threading.Lock()


def _get_db() -> TranslationDB:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = TranslationDB()
    return _db
