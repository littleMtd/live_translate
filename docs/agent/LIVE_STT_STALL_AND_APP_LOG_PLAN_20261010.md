# Live: bound ElevenLabs stalls and keep the app log (2026-10-10)

Author: Claude Code. Status: implemented; Codex PASS (round 3).
Scope: `config.py` (`_STT.elevenlabs_timeout`), `modules/stt.py` (ElevenLabs request options,
failure telemetry), `utils/logger.py` + `main.py` (file mirror of console logs), tests.

## 1. Evidence

- STT is one sequential worker; while one ElevenLabs request hangs, every later chunk waits.
- All `run_kind=live` ElevenLabs STT events: 9642 successes, latency p50 1.27 s, p99 2.44 s,
  p99.9 5.99 s, max 26.2 s; 9 successes took > 6 s. Failures: 5 at the 15 s client timeout,
  7 at 6–7 s (`reason=error`, cause unknown).
- The SDK (elevenlabs 2.64) retries 5xx/429/408/409 up to `max_retries` (default 2) with
  backoff; live STT never set it. **Candidate** explanation (events hold no per-attempt evidence)
  for failures after 6–7 s and successes after 12–26 s
  (e.g. run `20261001T141532Z-14024`: one 26.2 s success held the next chunk's request 24.8 s).
  Run `20261010T090328Z-2704` had three 11–16 s stalls.
- The failure reason/class was never recorded, and console log lines (where
  `ElevenLabs STT error: …` is printed) are not persisted, so the exception type is unknowable.

## 2. Change

1. `elevenlabs_timeout` 15.0 → 6.0 s (just above p99.9). This is HTTPX's per-operation
   timeout (connect / each read / each write / pool), **not a total deadline**: a request that
   stops responding fails 6 s after the last byte instead of 15 s, but a slow transfer that keeps
   making progress can still exceed 6 s. The observed 15.0x s failures were no-response stalls,
   which this bounds. The existing same-chunk Groq fallback and 30 s cooldown then take over.
2. ElevenLabs `convert(..., request_options={"max_retries": 0})`: no hidden SDK retries;
   failures go straight to the existing Groq fallback. The request contract
   (`request_parameters`) is unchanged — transport options are not model inputs.
3. Failure telemetry: the `stt` event gains `error_type` (exception class name) and
   `error_status` (HTTP status from `status_code` or `response.status_code`, else None);
   both are empty/None on non-failure events. The ElevenLabs log line is now class + status
   only — `ApiError.__str__` embeds response headers and body.
4. `utils.logger.enable_file_logging(log_dir, run_id, secrets)` adds one handler on the root
   logger writing `logs/app_YYYYMMDD.log` (append, UTF-8, console format), once per process.
   The file switches at local midnight; files older than 30 days are deleted at startup. Its
   formatter redacts `headers: {…}` and `body: …` segments and every configured API key
   (`cfg.keys.*`, ≥ 8 chars) from every line — including other modules' exception logs. If the
   directory cannot be prepared it warns on the console and returns None. `main.main()` calls it unless `runtime_events.run_kind == "test"`, so pytest
   (which calls `main.main()` in several tests) never writes into `logs/`. `logs/` is gitignored.

Trade-off: the ~0.1 % of chunks that ElevenLabs would have answered in 6–26 s are transcribed by
Groq instead (lower quality, and the 30 s cooldown keeps Groq for following chunks). Accepted:
a 6–26 s blackout is worse than one Groq-quality line.

## 3. Validation

- SDK check (no network): `convert` accepts `request_options`; `HttpClient.request` reads
  `max_retries` from it; client `get_timeout()` = 6.0.
- Tests: request carries `request_options == {"max_retries": 0}`; a 503 provider error records
  `error_type`/`error_status` and still falls back to Groq; status extraction handles
  `response.status_code`, rejects bool, returns None for timeouts; production timeout pinned
  at 6.0; the ElevenLabs log line carries no provider text; file logging writes the run marker
  and error lines to a dated file, is idempotent, redacts headers/body/keys, switches at
  midnight, prunes > 30-day files, warns when the dir is unusable; `main.main()` enables it only
  outside tests. Full suite: 1696 passed, 1 skipped; no `logs/app_*.log` created by the suite.
- Live check after merge: `logs/app_YYYYMMDD.log` appears; any ElevenLabs no-response failure
  shows `error_type`/`error_status` and `latency_ms` ≈ 6 s instead of 15 s.

## 4. Review log

### Codex review round 1 (2026-10-10): FIX

Blockers: (1) the console/file line logged the raw provider exception, and ElevenLabs
`ApiError.__str__` includes response headers and body (verified with synthetic cookie/body
values) — not an API-key leak, but a persisted-sensitive-data path; (2) 6 s is HTTPX's
per-operation timeout, not a total deadline, so "≤ 6 s" claims were wrong. Non-blocking: no
midnight switch or retention; silent loss when the file cannot be opened; the import test did not
test import; main's test guard untested; SDK retries stated as confirmed cause.

### Claude Code revision (round 2)

- ElevenLabs log line: class + status only. File formatter redacts `headers: {…}`, `body: …` and
  configured API keys on every line. Test with synthetic cookie, body token and key: none reach
  the file; `status_code` survives.
- Plan wording: per-operation timeout, not a deadline; SDK retries as candidate cause.
- `_DailyFileHandler` switches at local midnight (test drives the date across midnight);
  startup deletes `app_*.log` older than 30 days (test); unwritable dir warns and returns None
  (test). `main.main()` guard tested both ways (`run_kind` test → no file logging, live → called
  with secrets). The ineffective import test was removed.
- Full suite: 1696 passed, 1 skipped; no `logs/app_*.log` created by the suite.

### Codex re-review round 2 (2026-10-10): FIX

Both round-1 blockers resolved (no provider text reaches the log; timeout wording corrected).
New blocker: the handler used `delay=True`, so the first real open happened while logging the
start marker, outside `enable_file_logging`'s try/except — an unopenable file would raise out of
`main.main()` and leave a broken handler on the root logger. Non-blocking: the `stt.py` comment
still stated SDK retries as the confirmed cause.

### Claude Code fix after round 2

- `_DailyFileHandler` opens eagerly in its constructor (inside the try); on failure it warns
  ("App log file disabled: PermissionError"), returns None and adds no handler (test).
- The midnight switch is wrapped: any reopen failure goes to `handleError` and never raises into
  a pipeline thread (test).
- `stt.py` comment now calls SDK retries a candidate cause.
- Full suite: 1698 passed, 1 skipped; no `logs/app_*.log` created by the suite.

### Codex re-review round 3 (2026-10-10): PASS

Initial open failure is caught (returns None, no handler, `_file_handler` stays None); a failed
midnight reopen goes to `handleError` and later records recover. Verified in memory (7/7,
including real `handleError` with `raiseExceptions` True/False). No new blocker.
