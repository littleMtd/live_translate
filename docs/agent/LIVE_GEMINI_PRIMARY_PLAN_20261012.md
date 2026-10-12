# Live: Gemini 3.8 Flash as the primary live translator, raced against DeepSeek (2026-10-12)

Author: Claude Code. Status: proposal for cross review. Owner delegated priorities and budget
(2026-10-12); the owner asked to go deeper than the name-glossary profile and to find where
translation actually fails first.

## 1. Evidence

### 1.1 Where live translation fails (Gemini 3.8 Flash judge, all October live runs)

3224 subtitles, judged in 40-line windows with the profile glossary: ok 82.8 %, ASR error 7.2 %,
mistranslation 4.1 %, lost 3.0 % (largely fixed by the 10-11 rescue/name work), proper noun
1.5 %, domain knowledge 1.0 %, incomplete 0.3 %. Codex re-checked 60 random verdicts: 78 %
agreement; the judge over-attributes to ASR (5/60), so translation-side errors are if anything
under-counted. Severe mistranslations are mostly polysemy resolved by the situation the model does
not grasp (수정 crystal→修正, 탑 top→塔, 소스 OBS source→醬汁, 이사 director→搬家, 전 삶 previous
life→我的人生, 7강 +7 enhance→7強, 따다 learn choreography→摘), plus polarity/subject inversions
and literal slang (당근→蘿蔔).

### 1.2 Re-translation A/B (same production DeepSeek messages, blind judge)

225 flagged lines (mistranslation / domain / proper noun / incomplete / hallucination) + 200
random lines the judge had marked ok (controls). Every candidate judged independently, shuffled,
with opaque ids.

| variant | flagged ok | flagged sev≥2 | controls ok | latency p50 / p90 |
|---|---|---|---|---|
| A DeepSeek flash, 2-turn history (production) | 39.1 % | 90 | 91.0 % | 955 / 1180 ms |
| B DeepSeek flash, 8-turn | 43.1 % | 74 | 89.0 % | 964 / 1180 ms |
| C **Gemini 3.8 Flash (thinking low), 2-turn** | **82.7 %** | **13** | **94.0 %** | 1826 / 3372 ms |
| D Gemini, 8-turn | 82.7 % | 10 | 93.5 % | 2054 / 4023 ms |
| F DeepSeek V4 Pro, 2-turn | 50.2 % | 60 | 91.5 % | 1079 / 1411 ms |

Paired vs A — C: flagged better 121 / worse 15; controls better 15 / worse 9. Selection favours
any non-A variant on flagged lines but disfavours it on controls; C still wins on controls.
History length is not the bottleneck (B, D); model capability is. Gemini `thinkingLevel:"minimal"`
is rejected by the API (all calls failed), so "low" is the floor.

## 2. Design: race inside the existing DeepSeek engine

Changing the engine name would touch ~32 DeepSeek-specific branches (prompt contract, raw guard,
cost, provisional, request contracts). Gemini consumes exactly the DeepSeek message contract
(that is what the A/B measured), so the race lives inside `DeepSeekTranslationEngine`:

- Final translations (`translation_runtime` path): start the DeepSeek HTTP request and a Gemini
  `generateContent` request concurrently (two worker threads that only do HTTP and return raw
  data/exception — no contextvars, telemetry or shared state). Wait for Gemini up to
  `gemini_deadline_seconds` (2.5 s, bounded by the route timeout). Gemini success with non-empty
  text → use it; otherwise wait for DeepSeek (already in flight) and use it as today. Both failing
  → the existing failure path (Groq fallback chain unchanged).
- Provisional translations (`_call_provider` path) stay DeepSeek-only: their job is speed, and
  the final revision brings Gemini quality. They are identified by a new
  `RouteRequest.phase` field (default "final"; provisional routes are built with "provisional").
- All telemetry is recorded in the calling thread after the choice, through the existing
  `_set_last_engine_diagnostics` / capture path: `api_cost_usd` = DeepSeek + Gemini cost (both
  are billed), plus `primary_provider` (gemini|deepseek), `gemini_outcome`
  (ok|empty|error|deadline|disabled), `gemini_latency_ms`, `deepseek_latency_ms`. `model_name`
  and the request contract keep describing the DeepSeek route; the runtime event records which
  provider produced the text.
- Config (`_Translation`): `gemini_primary_enabled=True`, `gemini_model="gemini-3.8-flash"`,
  `gemini_thinking_level="low"`, `gemini_deadline_seconds=2.5`, pricing
  (0.75 / 3.75 USD per M tokens, revision "2026-10-12"); disabled automatically without
  `GEMINI_API_KEY`. Gemini 429/5xx simply loses the race (no retries).
- Post-processing, guards, rescue, cache, provisional promotion and history are unchanged: they
  see one candidate string, as today.

Expected latency: final-subtitle API time p50 ≈ 1.8 s (from 1.0 s), capped near 2.5 s; first
display is unchanged where a provisional is shown. Cost ≈ DeepSeek + Gemini per final sentence
(~US$0.2–0.4 per hour of streaming).

## 3. Validation

- Unit: race picks Gemini when it returns first/within the deadline; falls back to DeepSeek on
  Gemini error, empty text, or deadline; both failing returns None with diagnostics; provisional
  phase never calls Gemini; disabled/no-key path is byte-identical to today; telemetry fields and
  summed cost; no contextvar access from worker threads.
- Full suite; CI-like run without PortAudio.
- Real pipeline replay (`latency_replay.py`, 10-min 주르르 clip) with the race on vs the stored
  baseline: speech-onset → final latency, lost lines, and a judged quality sample of the finals.
- First live run: `primary_provider` share, `gemini_outcome` distribution, latency, cost.

## 4. Review log

### Codex plan review round 1 (2026-10-12): REVISE

Racing Gemini inside `DeepSeekTranslationEngine` breaks existing contracts: `phase` cannot scope
eligibility (probe, clip, offline, legacy routes); the per-attempt hard deadline/semaphore would no
longer bound real HTTP; diagnostics are whitelisted and would drop new fields; circuit-breaker
health semantics become ambiguous; costs of the losing request are unknown at return time; the
DeepSeek request contract cannot describe the Gemini wire request and cache identity lacks the
policy; provisional promotion would keep DeepSeek finals for most sentences; Gemini response
handling (finish reason, thought parts, truncation) is undefined.

### Claude Code revision (round 2): a pluggable primary provider, no race

Replace the race with a first-class engine and route, reusing the existing fallback-chain
machinery for deadlines, attempts, circuit breaking, telemetry and contracts:

1. **Primary provider setting.** `cfg.translation.primary_provider` ∈ {"gemini", "deepseek"}
   (default "gemini"; "deepseek" restores today's behaviour exactly). The protected live route
   ("primary" `deepseek_route`, the capsule prompt contract, raw guard, provisional, promotion) is
   served by the primary engine. Chain for live and clip: `[primary, other provider, groq]`
   (`effective_engine_chain_names`), so a Gemini outage or timeout falls back to DeepSeek, then
   Groq. Offline subtitles keep their own `GeminiTranslator`; legacy `translate()` entry points keep
   working through the engine's own route builder.
2. **`GeminiTranslationEngine`** (`engine_name="gemini"`): consumes the same message contract as
   DeepSeek (`build_effective_deepseek_messages` → `systemInstruction` + `contents`), its own
   frozen `RouteRequest` (Gemini body, options `model/thinkingLevel/maxOutputTokens/temperature`,
   `timeout_seconds`) built by a `primary_route_for_messages(engine, messages)` dispatcher, so the
   request contract, body hash, provider options and cache identity are Gemini's own.
   Response contract mirrors the offline adapter: exclude `thought` parts, require non-empty text and
   `finishReason == "STOP"` (MAX_TOKENS/SAFETY/other → empty result with the reason recorded),
   `maxOutputTokens = route max_tokens + 512` for thinking. Errors are classified through the
   existing `_classify_api_error`; 429 is `rate_limited`; no retries inside the adapter.
3. **Telemetry and cost** go through the existing `_set_last_engine_diagnostics` whitelist (no new
   field names: engine/model identify the provider); usage maps prompt/candidates/thoughts tokens to
   the existing usage keys with thinking counted as output for cost; cost at 0.75 / 3.75 USD per M
   (`gemini_pricing_revision`). Only the engine that ran is billed, as today.
4. **Deadlines and admission.** Gemini route timeout `gemini_timeout=3.0` s, inside the existing
   per-attempt hard-deadline/semaphore path, so a slow Gemini attempt is bounded exactly like a slow
   DeepSeek one and falls through to DeepSeek within the sentence budget.
5. **Provisional and promotion.** Every hard-coded `DeepSeekTranslationEngine()` used for live
   provisional/promotion work becomes `make_primary_engine()`; `deepseek_provisional_eligible`
   becomes primary-route eligibility (same conditions). Provisional and final both use the primary
   engine, so promotion still yields a Gemini final; the fingerprint already includes the route
   options/body, which now carry the Gemini model and thinking level.
6. **Raw guard.** The `is_deepseek` raw-output boundary becomes `is_primary_route` (engine in
   {"deepseek", "gemini"} on the protected route): same strictness for whichever provider answers.
7. **Recovery probe** probes route 0 = primary engine (Gemini); circuit/persistent switch semantics
   are unchanged per route.

Expected latency (A/B): primary API p50 1.8 s (+0.9 s), p90 ≈ 3.4 s, capped by the 3.0 s route
timeout then DeepSeek. This applies to provisional and final alike. Rollback: one setting.

Validation additions: route/contract/cache identity per provider; finishReason handling; chain
order and fallback on Gemini timeout/429/empty; provisional/promotion with Gemini; raw guard
applies to Gemini; `primary_provider="deepseek"` reproduces the pre-change request bodies byte for
byte; real-pipeline replay latency on the 10-minute clip; first live run review.

### Codex plan review round 2 (2026-10-12): REVISE

Round-1 blockers 1–3 and 5 no longer apply; 4, 6, 7, 8 resolved in direction but wiring
incomplete. New blockers: N1 `[primary, other, groq]` changes the DeepSeek-primary chain;
N2 provisional and clip calls do not go through the hard deadline; N3 Gemini content outcomes
(MAX_TOKENS/SAFETY) would count as provider failures, and 429 classifies as
`("api_error", "rate_limit")`; N4 `engine_chain_config_key`, registry, startup validation and
missing-key policy; N5 `scripts/replay_translation_contract.py` rejects a Gemini contract. Full
inventory of DeepSeek-specific sites (translator ×18 groups incl. 1441 accepted-evidence gate,
2582–2584 final route search, 3203/3208/3233/3792 contract maps; engines ×10 incl. 801
system-prompt dispatch, 1660 registry, 1689 config key; request ×3 incl. 161–183 freeze dispatch;
provisional 154; plus sentence_splitter 26/472 and translation_runtime 619/621 probe whitelist).

### Owner-delegated decisions (round 3, Claude Code) — implementation spec

- **N1 chain:** gemini primary → `gemini, deepseek, groq`; deepseek primary → `deepseek, groq`
  (exactly today); `deepseek_route="off"` → `groq`. The protected-route switch keeps its name.
- **N2 deadlines:** final translations keep the existing hard deadline/admission. Provisional and
  clip calls keep today's transport-timeout-only behaviour for whichever primary runs; documented,
  not over-claimed. The final subtitle is not capped at 3 s overall (Gemini attempt ≤ 3 s, then
  DeepSeek).
- **N3 outcome table (final and probe identical):** transport/timeout/connection/429/5xx/
  unparseable → provider failure through `_classify_api_error` (429 stays
  `("api_error","rate_limit")`); a valid response with finishReason ≠ STOP (MAX_TOKENS, SAFETY,
  RECITATION, OTHER), STOP with empty text, or no candidates → content-scope outcome that must not
  open the circuit or reset/extend probe cooldown differently from a DeepSeek content rejection;
  finish reason carried through `EngineResult` metadata.
- **N4:** registry entry for gemini (`GEMINI_API_KEY`); `primary_provider` and every Gemini
  adapter setting in `engine_chain_config_key`; missing Gemini key → effective primary DeepSeek
  with a startup warning (startup validation checks the effective first engine); provider switch
  rebuilds the chain, resets fallback state and rejects provisional candidates from the old
  provider (engine/model check at promotion).
- **N5:** contract replay reconstructs Gemini bodies and validates hashes; old contracts still
  replay.
- Inventory above is the checklist; raw guard (both rejection and evidence branches, plus the
  accepted-evidence gate) applies to the answering primary-route provider via an explicit
  protected-route flag, not the active index.

### Implementation (Codex, round 3) and verification (Claude Code, 2026-10-12)

- Full suite green in the normal and the CI-like environment (fake `sounddevice`, empty keys);
  one CI-only failure fixed by pinning the DeepSeek primary in the startup-warning test.
- Fresh Codex post-implementation review: FIX-THEN-SHIP. Blocker (N4): a request in flight on the
  old chain could merge its `active_idx` into the rebuilt shared state (Codex reproduced an
  `IndexError` with two shared translators). Fixed: `FallbackState.chain_key`; only the first
  rebuild per key resets; merges and probes from another chain key are dropped; a request whose
  worker has not refreshed yet runs on its own chain and never merges. Regression tests added.
  Non-blockers: transport block now uses `pytest.fail` (adapters cannot swallow it); finish
  reason on provisional/probe events left as a follow-up.
- Real-pipeline replay (`latency_replay.py`, jururu 610 s and soop): the "gemini" run is invalid —
  every Gemini call returned HTTP 402 "prepayment credits are depleted", so each sentence paid a
  failed call and fell back to DeepSeek, and all 33 provisionals failed (first subtitle p50
  6.5 s → 8.0 s). The 402 was classified `http_4xx` (unknown scope), so the circuit never opened.
  Fixed: HTTP 402 → `billing_error`, a provider failure that opens the circuit; other 4xx and auth
  errors keep their earlier "do not open the circuit" behaviour.
- The DeepSeek run on the new code matches the pre-change baselines within run-to-run noise
  (coverage 0.71 vs 0.68/0.69; first p50 6.48 vs 6.33/6.25 s; final p50 7.08 vs 7.22/7.01 s).
- **Decision:** ship the provider plumbing with `primary_provider="deepseek"` (behaviour
  unchanged). Switching to Gemini needs (1) the owner to top up Gemini prepaid credits and
  (2) a valid real-pipeline replay. Open item for that replay: `gemini_timeout=3.0` sits below the
  A/B p90 of 3.37 s, so roughly one final in ten may time out and pay DeepSeek on top; measure the
  fallback share before switching.
- Codex re-review rounds 2–3 (fallback-state isolation): the first fix (config key only) left
  A→B→A reuse and late-worker gaps. Replaced with `FallbackState.chain_generation` (bumped on
  every chain change, compared on every merge), `_sync_shared_chain` at construction and refresh,
  `_consistent_engine_chain` (key read before and after the build), and probes skipping a chain
  that does not match the shared one. Round 3 confirmed the shared-state ABA and late-worker
  cases closed. Remaining, accepted: an A→B→A config flip *during* one chain build can still pair
  a chain with the wrong key. Production config is fixed at import (`config.py` applies dashboard
  overrides once), so no runtime path mutates provider settings, and the pre-change refresh had the
  same build window. Revisit only if live provider switching is ever added (then build the chain
  and key from one immutable config snapshot).
- Known limitation carried over: provisional requests call the primary directly and are not
  gated by the circuit, so with an unhealthy Gemini every provisional still pays one failed call.

### Third-party blind re-judge and owner decision (2026-10-12)

The A/B above was judged by Gemini, which also wrote variant C. Re-judged blind with Codex
`gpt-6-luna` (high reasoning; calibration on 50 items: luna–sol 78%, luna–Gemini 84%,
sol–Gemini 86% ok/not-ok agreement; all three ranked C above A):

| Judge | Flagged 225: A → C ok | Flagged sev≥2 | C better / worse | Controls 200: A → C ok |
|---|---|---|---|---|
| Gemini (self) | 39.1% → 82.7% | 90 → 13 | 121 / 15 | 91.0% → 94.0% |
| Codex luna | 51.1% → 71.6% | 87 → 44 | 84 / 30 | 88.5% → 89.0% (16 / 16) |

The Gemini judge roughly doubled the gain (self-preference plus the flagged set being chosen by
the same judge). The real gain is about one extra correct line in thirty, with Gemini worse on 30
of the 225 flagged lines, for ~+0.9 s p50 latency and an estimated US$4–5 per 3-hour stream.
**Owner decision: stay on DeepSeek; do not switch.** The provider switch stays in the code at
`primary_provider="deepseek"`.
- Follow-up (owner, 2026-10-12): remove the unused live Gemini path. 39fbb29 reverted; kept only
  the provider-agnostic pieces: HTTP 402 → `billing_error` opens the circuit, and the autouse
  test fixture that fails any unmocked `urllib.request.urlopen`.
