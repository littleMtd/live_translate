# Offline subtitles: ElevenLabs Scribe for transcription (2026-10-09)

Author: Claude Code. Status: proposal for cross review.
Scope: `scripts/make_subtitles.py transcribe` and `modules/offline_subtitles.py` only. The live STT path is
unchanged (it already uses ElevenLabs `scribe_v2` as primary).

## 1. Evidence (user-authorized paid A/B, same audio, same profile vocabulary)

Script: scratchpad `stt_ab.py` (not in repo). Groq `whisper-large-v3` got the profile STT prompt (as today);
ElevenLabs `scribe_v2` got the same registry terms as `keyterms`, `language_code=ko`, word timestamps.

| Clip | Groq whisper-large-v3 | ElevenLabs scribe_v2 |
|---|---|---|
| 랑코 78 s (stream talk, Naver upload) | **First ~30 s missing**; begins with a hallucinated line not in the audio | Complete from the start |
| SOOP 31 s (stream talk) | All lines present; errors: 한정미화원, 낚시떠핑 뜯고 | 환경미화원, 낚시터 삥뜯고 correct; dropped interjections (흥 흥, 하하), one extra short phrase at the end |
| 고세구 MV 177 s (singing, official ko lyrics) | **First verse missing**; YouTube-style hallucination at start; chorus 세상을 구하자 correct | First verse nearly exact; chorus misheard (세상을 거덜 내, 쌀그릇) |

CER vs official lyrics (MV): Groq 0.586, ElevenLabs 0.615 — both poor on music, not decisive.
Decisive difference: Whisper drops whole passages and inserts hallucinated lines; Scribe did not.
Latency (not important offline): Groq 2.2–5.6 s, Scribe 3.1–10.3 s per clip.

User decision (2026-10-09): switch offline transcription to ElevenLabs; keep Groq as an explicit option.

## 2. Design

- **E1 CLI**: `--stt elevenlabs|groq`, default `elevenlabs`. No automatic fallback between providers (same
  policy as `--translator`). `--estimate` builds no provider and needs no key.
- **E2 Key checks before any paid call**: the selected STT key and the selected translator key are both checked
  before the first transcription request (today only the translator key and Groq are checked).
- **E3 Slicing unchanged**: keep `slices()` (600 s + 5 s overlap, midpoint ownership, last slice owns past the
  end) and `segment_cues()` so both providers share ownership/dedup rules. Each slice is a 16 kHz mono WAV as today.
- **E4 Request**: `speech_to_text.convert(file, model_id=cfg.stt.elevenlabs_model, language_code="ko",
  timestamps_granularity="word", tag_audio_events=False, diarize=False, keyterms=...)`, client
  `timeout=OFFLINE_STT_TIMEOUT_SECONDS` (300 s). Keyterms: registry common terms + the profile's terms, de-duplicated,
  same character/length filters as the live path (`_ELEVENLABS_UNSUPPORTED_KEYTERM_CHARS`, < 50 chars, ≤ 5 words),
  at most 100. No activity terms (offline jobs bind an empty activity).
- **E5 Words → segments** (then the existing `segment_cues`): drop non-`word` tokens except that `spacing` joins
  text; start a new segment when the gap to the previous word is ≥ 0.8 s, or the current segment already ends
  with sentence punctuation (`.?!…`) and lasts ≥ 1.0 s, or it would exceed 6.0 s or 42 characters. Words with
  missing/non-finite/negative times or `end < start` are skipped. Segment times are slice-relative (as Groq's).
- **E6 Errors** (mirrors the Gemini R1′ split; fixed messages, never provider text):
  - 401 / 403 → abort: 「ElevenLabs 金鑰無效、沒有權限或額度不足（HTTP 401）」.
  - 400 / 404 / 422 → abort with status only.
  - 429 / 5xx / network → bounded retry with `Retry-After`, one budget for the whole job; exhausted → abort.
  - Malformed response for a slice → abort (no silent gap; a missing slice would be worse than a failed job).
- **E7 Report**: `stt_provider`, `stt_model`, `stt_keyterms` (count). Groq path unchanged.
- **E8 Estimate**: `stt_cost_usd` uses a dated per-hour constant for `scribe_v2` taken from ElevenLabs' current
  public API pricing (reviewer to verify the number and date); unknown model → `null`. Billed seconds follow the
  same slice sum as today (overlap included).
- **E9 Leak safety**: the SDK exception is never printed; only status/class-level fixed text. Tests use a fake key
  and check stdout, stderr, logs and report (same harness as the Gemini work).

## 3. Tests (all mocked; no network)

- Words→segments: gaps, punctuation, 6 s / 42-char caps, spacing joins, bad timestamps skipped, audio events ignored.
- Ownership across slices with Scribe segments (overlap words owned once).
- Keyterm filtering and the 100 cap; offline uses no activity terms.
- Error matrix: 401/403/400/404/422 abort once; 429/5xx retry with shared budget; malformed slice aborts.
- CLI: default provider is ElevenLabs; `--stt groq` keeps today's behaviour; missing STT key fails before any request;
  `--estimate` needs no keys; fake-key leak test across stdout/stderr/logs/report.
- Live import chain still does not load `modules.offline_subtitles`.

## 4. Assumptions to verify

- A1: Scribe accepts 600 s WAV slices within 300 s (A/B: 177 s clip in 10.3 s).
- A2: ElevenLabs status codes for invalid key vs exhausted credits (reviewer: check current docs).
- A3: Post-implementation real check on the same three clips: cues complete from the first second, no hallucinated
  lines, timing within ±0.5 s of audible speech on spot checks.

## 5. Review log

### Codex plan review round 1 (2026-10-09): REVISE (review environment)

Codex's Windows sandbox failed to start (`setup refresh had errors`), so it could not read the repo; it verified
official docs only (checked 2026-10-09):
- Invalid key: HTTP 401 `code=invalid_api_key`. Rate/concurrency: 429 `rate_limit_exceeded` / `concurrent_limit_exceeded`.
- Credits exhausted: current table 402 `type=payment_required`, `code=insufficient_credits`; the Help Center still
  documents the legacy `quota_exceeded` (401). Classify by `detail.code` and legacy `detail.status`, not status alone.
- Price: Scribe v2 batch US$0.22 per audio hour; keyterm prompting +US$0.05 per hour (realtime is US$0.39 — not used).

### Claude Code revision (round 2)

- **E6′**: 402, or any 4xx whose body `detail.code`/`detail.status` is `insufficient_credits` / `quota_exceeded` →
  abort with 「ElevenLabs 額度不足，請到 ElevenLabs 加值或升級方案」. 401/403 otherwise → 「ElevenLabs 金鑰無效或沒有權限」.
  429 (both codes) / 5xx / network → shared-budget retry. Other 4xx → abort with status. The body is read only to
  pick one of these fixed messages; no provider text is ever shown. Tests: 401+invalid_api_key, 401+quota_exceeded,
  402+insufficient_credits, 429 rate and concurrent, 500, network.
- **E8′**: `ELEVENLABS_SCRIBE_V2_USD_PER_HOUR = 0.22`, `ELEVENLABS_KEYTERMS_USD_PER_HOUR = 0.05`,
  `stt_pricing_revision = "2026-10-09"`; cost = billed hours × (0.22 + 0.05 when keyterms are sent); other models → null.

### Codex plan review round 2 (2026-10-09): REVISE

Reviewed from embedded plan + code (sandbox could not read files). E6′ matches the documented codes.
- B1: grouping words per slice and then keeping whole segments by midpoint can drop words on both sides of the
  600 s boundary (e.g. prior slice 600.0–604.2 → midpoint 602.1 dropped; next slice 595.0–600.2 → 597.6 dropped).
- B2: no distinction between a legitimately silent slice and a malformed/unusable one; a broken middle slice could
  leave a silent gap because the CLI only rejects a job with no cues at all.
- Non-blocking: shared retry budget must be defined (attempts and/or seconds; today both reset per slice) and SDK
  retries must not multiply it; `_retry_after` must read the real SDK exception's headers; estimate must not reuse
  Groq's 10 s minimum and the CLI must pass provider/model/keyterm use; single words longer than the caps;
  punctuation followed by `spacing`; leak test must cover exception message, body and headers and file logs;
  verify a 610 s middle slice after implementation.

### Claude Code revision (round 3 proposal)

- **R-B1 ownership per word, grouping once globally.** For each slice, convert word times to absolute and keep a
  word only if its midpoint is in `[owner_start, owner_end)` (same rule `segment_cues` applies to Groq segments).
  Owned words from all slices are concatenated in time order, then grouped by E5 **once over the whole job**. The
  ElevenLabs path does not call `segment_cues`; cues are clamped to `[0, duration]`. Because every word is owned by
  exactly one slice and grouping never discards words, no word is lost or duplicated regardless of slice layout.
  Test: one synthetic word stream cut with different slice lengths/overlaps (incl. a word straddling 600 s) yields
  the identical cue list, and the multiset of words equals the input.
- **R-B2 response validation.** A slice response must have `text` (str) and `words` (list of objects with `type`).
  Legitimate silence: no `word` tokens and empty/whitespace `text` → zero words, OK. Malformed → abort the job with
  a fixed message: missing/ill-typed `text` or `words`; non-empty `text` but no `word` token with valid finite
  `start ≤ end`; more than 20 % of `word` tokens with invalid times. Tests: silent middle slice succeeds; malformed
  middle slice (between two good ones) aborts and delivers nothing.
- **Retry budget.** Per request at most 3 retries; total wait across the job ≤ 300 s; both counters live on the job
  (not per slice). The ElevenLabs call uses `request_options={"max_retries": 0}` so the SDK does not retry on its own.
  `_retry_after` reads headers from the SDK `ApiError` (`.headers`) as well as urllib/Groq shapes; tested with the
  real exception class.
- **Estimate.** ElevenLabs billed seconds = sum of slice lengths (no 10 s minimum); cost uses 0.22 + 0.05 when the
  profile yields at least one keyterm. The CLI passes `--stt` provider, its model and the keyterm count to `estimate`.
- **Caps.** A single word longer than 6 s or 42 characters becomes its own cue (no splitting). Sentence punctuation
  is checked on the last `word` token, ignoring trailing `spacing`.
- **Leak test.** Fake key placed in the exception message, response body and headers; assert absent from stdout,
  stderr, `caplog`, the repo log file handler output and the report. No SDK text is ever wrapped into
  `OfflineSubtitleError`.
- **A1′.** After implementation, run one real ≥ 610 s middle slice (a ~20 min VOD) and record the request time.

**User decision (2026-10-09)**: implement the round-3 revision. Implementer: Claude Code; Codex reviews after implementation.

## 6. Implementation checks (Claude Code, 2026-10-09)

- Full pytest: 1667 passed, 2 skipped. Offline tests cover word ownership across four slice layouts (identical cues,
  no lost/duplicated words), silent vs malformed middle slice, the error matrix with the real `ApiError` class,
  the shared retry budget across slices, keyterm rules (constant equals the live one), pricing, key checks before
  any request, and a fake-key leak test through the CLI.
- A3 (real, paid): 랑코 78 s → 20 cues starting at 0.06 s (the ~30 s Groq dropped is present); SOOP 31 s → 11 cues
  (환경미화원 correct). Gemini translations for both, no failed cues.
- A1′ (real, paid): a 24-minute file → slices 0–605 / 595–1205 (610 s) / 1195–1430; per-slice request time
  ≈ 24 s / 21 s / 13 s (limit 300 s); 440 cues; cues across 600 s and 1200 s appear once, no overlaps.

### Codex post-implementation review (round 1): FIX → Claude Code fixes

- B1 retry counters: the contract is now explicit — at most 3 retries per request **and** at most 6 retries in the
  whole job, plus ≤ 300 s total wait; both counters live on the job. Test: retries spread over slices hit the 6 cap.
- B2 clamping: cues stay inside `[0, duration]` even for a zero-width word at the very end (start is pulled back so
  the 0.05 s minimum fits). Test: duration 1.0, word 0.99–0.99 → 0.95–1.0.
- Tests: leak test also checks a real file log handler and no longer sleeps (`sleep=None` resolves `time.sleep` at
  call time); CLI cases normal/silent/normal (delivers) and normal/malformed/normal (no files); Groq `--stt groq`
  success path end-to-end with fake Groq and Gemini.

### Codex post-implementation review (round 2): PASS

All round-1 items resolved, no new blocker. Its suggestion (exact assertion for a lone zero-width word: 0.95–1.0) was added.
