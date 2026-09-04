# Database Requirements

## Purpose

Add a lightweight local database layer for persistent translation cache and usage tracking.

This project is a single-machine live subtitle system, so SQLite is the preferred first implementation.

This file is the only source of truth for database behavior.
Do not duplicate these rules in `system.md`.

## Goals

1. Persist translation results across restarts
2. Reduce repeated API calls for the same sentence
3. Track usage statistics for cost analysis
4. Store prompt evolution metadata for future inspection
5. Keep the database layer isolated from the rest of the pipeline

## Phase 1 Scope — ✅ Implemented

Persistent translation cache only. Stream session metadata and prompt-evolution tables (listed under *Suggested Tables* below) are still future work.

When persistent cache is enabled for the active mode, the translator stores a
record on successful final translation and may reuse it when the same sentence
reappears under the same engine/model/prompt version. Live DB cache is disabled
by default; clip mode may enable it.

Use the cache key:

- `source_text`
- `target_lang`
- `model`
- `engine`
- `prompt_version`

**Important:** Only cache complete translations (`incomplete=False`). Incomplete sentences should remain in memory cache only.

If a record exists, return the cached translation immediately and update usage metadata.

If the DB is unavailable, the app must continue running with the existing in-memory cache and current pipeline behavior.

## Implementation Constraints

- Database engine: SQLite only for Phase 1
- Database file location: store inside the project `logs/` folder or another clearly documented local path
- Access layer: keep SQL isolated in `modules/db.py` or `utils/db.py`
- Migration strategy: include a schema version field or metadata table from the start
- Unique key rule: enforce one row per logical cache entry using a unique constraint on the cache key columns
- Normalization rule: trim text and collapse repeated whitespace before storing or comparing keys
- Fallback rule: if DB calls fail, the in-memory cache and live pipeline must still work
- **No incomplete sentences:** never write rows with `incomplete=True` to the database. Keep incomplete translations in the in-memory cache only to avoid cache pollution.

## Thread Safety

### Design Philosophy

The current system uses two translation workers. `TranslationDB` owns one
SQLite connection opened with `check_same_thread=False` and serializes all DB
operations with an `RLock`; callers must not bypass that owner.

### Thread Safety Strategy

1. **SQLite WAL Mode (Write-Ahead Logging)**
   - Enable WAL mode in the database connection: `PRAGMA journal_mode=WAL`
   - WAL allows concurrent reads while a single writer is active.
   - Suitable for single-writer, multiple-reader scenarios (e.g., if inspection tools query the DB later).

2. **Serialized connection owner**
   - Translation workers may reach the shared DB concurrently.
   - `TranslationDB` holds its `RLock` around lookup, store, delete, and each
     eviction operation. `close()` is currently an unlocked shutdown action.
   - The in-memory translation cache has its own synchronization owner in
     `modules/translation_memory.py` / translator shared state.

3. **Transaction Isolation**
   - Each translation write uses one transaction: read → check cache key → insert/update → commit.
   - Prevents partial rows in the database.
   - Committed transactions are immediately visible to all readers.

### Future Considerations

If a future phase adds new DB writers outside the current translation workers
(for example a background statistics updater):

- Switch to explicit connection-level locks (e.g., `threading.Lock` around each DB operation).
- Or use connection pooling with thread-safe wrappers (e.g., `sqlalchemy` or `peewee`).
- Or migrate to PostgreSQL for better multi-writer support.

### Phase 1 Scope

- **Do:** implement WAL mode ✅
- **Do:** serialize worker DB operations through `TranslationDB` ✅
- **Do:** use transactions for data consistency ✅
- **Do:** use `threading.RLock()` around shared connection operations; eviction
  takes its own short lock after the store commit ✅
- **Do not:** attempt to optimize reads from other threads (not a bottleneck)

## Suggested Tables

### translations — ✅ Implemented (`modules/db.py`)

Fields:

- `id`
- `source_text`
- `target_text`
- `source_lang`
- `target_lang`
- `engine`
- `model`
- `hit_count`
- `created_at`
- `last_used_at`
- `prompt_version`

Recommended constraints:

- unique index on (`source_text`, `target_lang`, `engine`, `model`, `prompt_version`)
- index on `last_used_at`
- index on `hit_count` if ranking hot entries becomes useful

### stream_sessions — 📋 Planned (not yet implemented)

Fields:

- `id`
- `started_at`
- `ended_at`
- `source`
- `notes`

Recommended constraints:

- one row per live session
- index on `started_at`

### prompt_evolution — 📋 Planned (not yet implemented)

Fields:

- `id`
- `session_id`
- `slang_json`
- `stream_context`
- `corrections_json`
- `created_at`

Recommended constraints:

- foreign key to `stream_sessions.id`
- index on `session_id`

## Cache Key Reconciliation

The DB cache and in-memory cache use **different keys intentionally**:

| Layer | Key | Scope | Stores incomplete? |
|-------|-----|-------|-------------------|
| In-memory (`modules/translation_memory.py::TranslationMemory.cache`) | `(text, incomplete, prompt_ver)` | Session only | Yes (fast path for all input) |
| DB `translations` table (`modules/db.py`) | `(source_text, target_lang, engine, model, prompt_version)` | Persistent | **Never** |

The DB key includes `engine`, `model`, and `prompt_version` so that cache entries are invalidated when the engine, model, or system prompt changes.

**Lookup order (as implemented in `modules/translation_memory.py::lookup_existing_event`):**

1. Check in-memory cache with `(text, incomplete, prompt_ver)` — return immediately on hit (counts as `memory_hit`).
2. If `incomplete=True` or no active engine, skip DB — never read or write incomplete sentences to DB (counts as `skipped`).
3. Check DB with `(source_text=text, target_lang, engine, model, prompt_version)` — on hit, update `hit_count` + `last_used_at`, mirror into in-memory cache, return as `db_hit`.
4. On miss return `miss`; the translator then calls the API.
5. On API success, store the result to in-memory cache always (both complete and incomplete).
6. On API success, store the result to DB **only if `incomplete=False`**.

The exact source label (`memory_hit` / `db_hit` / `skipped` / `miss`) is carried out of `lookup_existing_event` as a `MemoryLookup` dataclass and surfaces in `runtime_events.jsonl` as the `cache_status` field.

**DB eviction:** apply LRU-style eviction based on `last_used_at` when row count exceeds a configurable limit (default: 50,000 rows). Configured in `config.py` as `cfg.database.db_cache_max_rows` (in `_Database` dataclass).

## Behavior Requirements

- Read from DB before calling the translation API.
- Write successful translations into DB **only if the sentence is complete** (`incomplete=False`).
- Keep incomplete translations in the in-memory cache temporarily; do not persist them to the database.
- Increment `hit_count` on cache hits.
- Update `last_used_at` on cache hits.
- Do not store failed API attempts as valid cache entries.
- Keep the DB logic in a separate module such as `modules/db.py` or `utils/db.py`.
- Keep the interface simple enough to later swap SQLite for PostgreSQL.
- Keep cache reads and writes read-through / write-through, not batch-only.
- Use one transaction per translation write to avoid partial cache rows.
- Preserve existing in-memory cache behavior as a first-line fast path.

## Non-Goals For Phase 1

- Do not store raw audio chunks.
- Do not store every queue event.
- Do not redesign the pipeline around the database.
- Do not require the database to be available for the app to start.

## Suggested Acceptance Criteria

- Repeated sentences hit the DB cache after the first successful translation.
- Restarting the app still allows old translations to be reused.
- Cache misses still call the existing translation flow.
- If the DB is offline, the app still works with memory cache only.
- **Incomplete sentences never appear in the database.** Only complete translations are persisted.
- Basic tests verify insert, lookup, hit-count update, and fallback behavior.
- Schema creation can run multiple times without breaking existing data.
- Cache normalization produces consistent hits for equivalent whitespace variants.

## Recommended Implementation Order

1. Create the SQLite schema
2. Add a small DB access module
3. Integrate read-through cache logic into the translator
4. Add tests for cache hit/miss and fallback behavior
5. Add usage/statistics tables after the cache path is stable

## Final Decisions (Implemented)

- DB path: `cfg.database.db_path` (default: `logs/live_translate.db`).
- `prompt_version`: MD5 hash (8 chars) of the evolved system prompt, computed per translation call.
- Cache eviction: LRU by `last_used_at` when row count exceeds `cfg.database.db_cache_max_rows` (default 50,000).
- Schema versioning: `schema_meta` table with `key='schema_version'`; current version is `"2"`.
- Migration v1→v2: added `prompt_version` column and updated UNIQUE constraint to include it.
- Lock: `threading.RLock()` serializes shared connection operations. `store()`
  commits and releases its lock before `_evict_if_needed()` takes a separate
  short lock.
