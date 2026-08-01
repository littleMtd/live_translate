# Optimization Roadmap and Progress

This is a binding routed extension of `AGENTS.md`. Read it completely for
optimization planning, TODO selection/advancement, evidence-gate decisions, or
progress updates.

## Incremental Optimization TODO (2026-07-24)

The user wants the remaining optimizations implemented slowly and
sequentially. Do not combine several TODO cards into one implementation.
Complete, validate, review, and observe one card before starting the next.
Keep this checklist synchronized when a card is completed, revised, deferred,
or rejected.

Per-card workflow:
1. Implement one TODO card only.
2. Run targeted tests.
3. Run `scripts/check_translator_core.py` and the relevant full/frozen checks.
4. Have a separate agent perform the post-implementation review.
5. Keep the card in one focused commit; never include the user's local
   `config.py` profile change.
6. Collect a real runtime run.
7. Validate with existing analyzers/harnesses instead of creating a bulk
   manual-labeling task.
8. Advance only after the runtime gate passes. Fix or roll back the current
   card if it does not pass.

Latest baseline used to order the work:
- Live run `20260724T134422Z-224064` lasted 2,859.579 seconds.
- 358 STT events and 261 translation events.
- STT latency: p50 563 ms, p95 1,844 ms, p99 4,235 ms.
- Translation latency: p50 1,031 ms, p95 2,016 ms, p99 3,890 ms.
- Translation queue wait p95 was 0 ms; adding workers is not a priority.
- Sentence cuts: 75/261 forced, including 39 incomplete forced blobs.
- NVIDIA produced 222 selected translations and DeepL 33. Only two NVIDIA
  read timeouts were recorded, but the circuit opened three times.
- STT prompt context was included whenever present (310/310), and the glossary
  was never truncated; expanding the STT prompt budget is not a priority.
- The OpenRouter production-shaped benchmark found that Qwen3-Next 80B with a
  compact domain capsule averaged about 513 input tokens/call and projects to
  roughly NT$75 per 100 runtime hours at the observed workload. A larger GPT
  model did not justify its much higher cost or tail latency.

Ordered cards:

- [x] **T01 - Provider-aware circuit breaker**
  - Only provider/transport failures may count toward a hard circuit switch:
    timeout, rate limit, HTTP 5xx, connection/transport error, parse failure,
    and genuinely empty provider response.
  - Content-specific outcomes such as `rejected_output`, untranslated output,
    and meta output may soft-fallback the current sentence but must not change
    `active_idx` or put later sentences on DeepL.
  - Do not change cooldown or recovery thresholds in the same card.
  - Tests must prove that content rejection does not advance the circuit and a
    real timeout still does.
  - Runtime gate: circuit openings are attributable to provider failures, and
    DeepL is not selected for a cooldown window after a content-only rejection.
  - Implementation completed 2026-07-24:
    - Attempts are classified as `provider`, `content`, `unknown`, or `none`.
      Timeout, connection, parse, rate limit, HTTP 5xx, explicit empty response,
      and a completed API attempt with genuinely empty content count as
      provider failures. Rejected output is content; auth/general 4xx and
      undiagnosed empty outcomes do not open the circuit.
    - Live circuit persistence advances only across contiguous provider
      failures. A later engine may provide the current sentence after an
      intermediate content rejection without becoming the next active engine.
    - A stale translation worker cannot advance past a newer shared active
      engine. Clip/offline behavior remains legacy-compatible and still
      persists the engine that actually succeeds.
    - Circuit events record `failure_scope`, `api_error_type`, and
      `api_error_message_class`; `analyze_runtime_events.py` groups circuit
      openings by those fields.
    - Validation: targeted 231 passed / 148 subtests; translator core 338
      passed / 148 subtests; full suite 940 passed / 4 skipped / 194 subtests;
      frozen replay 750 cases / 0 divergence; `git diff --check` passed.
    - Independent post-implementation re-review: PASS after fixing the stale
      worker multi-hop advancement blocker. No correctness blockers remain.
  - Live runtime gate remains unverified in this document. On 2026-07-25 the
    owner explicitly authorized continuing after post-implementation review;
    do not retroactively describe that owner override as runtime validation.

- [x] **T02 - Legitimate preserve-as-is acceptance**
  - Permit identical source/target output for narrowly recognized acronyms,
    URLs, IDs, brands, official titles, and profile-preserved Korean stage
    names.
  - Do not accept an ordinary Korean sentence copied unchanged.
  - The `A.I.N.D.S.` incident must no longer cascade through NVIDIA, DeepL,
    Groq, and OpenRouter before failing.
  - Preserve-as-is classification must remain fail-closed when ambiguous.
  - Implementation completed 2026-07-25:
    - Identical output is accepted only for narrow machine-readable shapes
      (dotted acronyms, URLs/domains, email/handles, numeric literals, and
      underscore/alphanumeric IDs), canonical shared STT acronyms, or terms
      proven by the active profile.
    - Profile evidence is derived from explicit self-mappings / `keep`
      directives in the canonical standard profile plus profile-scoped
      canonical name-rendering rules. It is disabled when `use_profile=False`;
      aliases whose required output differs remain rejected.
    - Ambiguous standalone ASCII title words remain fail-closed. In particular,
      URL profile `Again` is rejected when copied unchanged, while
      `Chemical Love` and `Wish Me Love` are accepted from the explicit
      official-title rule.
    - `A.I.N.D.S.` now succeeds on the first identical provider result; the
      NVIDIA / DeepL / Groq / OpenRouter regression test proves that later
      engines receive zero calls and the circuit remains on the primary.
    - Validation: targeted 234 passed / 158 subtests before review; final
      focused translator core 346 passed / 166 subtests; full suite 948 passed
      / 4 skipped / 212 subtests; frozen replay 750 cases / 0 divergence;
      `git diff --check` passed.
    - Independent post-implementation review initially found the ambiguous
      `Again` acceptance; after correction and new boundary tests, re-review
      PASS with no remaining correctness blocker.

- [x] **T03 - OpenRouter Qwen capsule fallback**
  - Use `qwen/qwen3-next-80b-a3b-instruct` with the tested compact domain
    capsule for OpenRouter.
  - Keep NVIDIA NIM on its current full prompt.
  - Target fallback order:
    `NVIDIA -> OpenRouter Qwen -> DeepL -> Groq`.
  - Validate the 40-case runtime harness, profile mappings, token telemetry,
    fallback attribution, and timeout behavior.
  - Runtime gate: record OpenRouter latency, prompt/output tokens, actual cost,
    and profile/correction behavior before accepting the card.
  - Implementation completed 2026-07-25:
    - Default live chain is now
      `NVIDIA -> OpenRouter -> DeepL -> Groq`. OpenRouter uses
      `qwen/qwen3-next-80b-a3b-instruct`, an 8-second timeout, 160 max output
      tokens, and `reasoning.effort=none` with reasoning excluded.
    - The production OpenRouter capsule is byte-for-byte identical to the
      tested `scratch/analysis/openrouter_prompt_economy_probe.py` capsule
      (URL profile: 1,896 chars, SHA-256
      `b8a1920e8f8aa1ced6a91d9f66d6f44cb3113e6616629b4d12e84e66b5af5d8d`).
      NVIDIA prompt selection was not changed and remains the full Qwen prompt.
    - OpenRouter response diagnostics now preserve `usage.cost` as
      `api_cost_usd`; attempt chains and selected runtime events carry it.
      `analyze_runtime_events.py` totals cost by engine using the attempt chain
      when present and the top-level event otherwise, avoiding double count.
    - Fresh 40-case API gate:
      `scratch/analysis/t03_qwen_capsule_runtime_20260725.json`; 40/40 success,
      zero quality flags, zero profile failures, average 512.8 prompt and 30.1
      completion tokens, latency p50 898 ms / p95 2,344 ms / max 3,547 ms,
      observed sample cost US$0.00339004, projected 100-runtime-hour cost
      US$2.42 / about NT$79.
    - Validation: targeted 294 passed / 166 subtests; translator core 349
      passed / 166 subtests; full suite 952 passed / 4 skipped / 212 subtests;
      frozen replay 750 cases / 0 divergence; production/harness prompt exact
      comparison and `git diff --check` passed.
    - Independent post-implementation review: PASS. It verified prompt
      identity, NVIDIA full-prompt isolation, chain/circuit behavior,
      timeout/attribution, token/cost telemetry, and analyzer non-duplication.

- [x] **T04 - Quality-signal false-positive cleanup**
  - Separate profile-approved preserved Hangul from unexpected Hangul leakage.
  - Separate official Latin terms such as Spotify, KTV, IDs, and titles from
    suspicious high-Latin output.
  - This card changes telemetry/classification only, not user-visible routing.
  - Implemented 2026-07-25:
    - Runtime translation events now add `quality_classifications` plus
      approved/unexpected Hangul and Latin spans. Existing `quality_flags`,
      score, severity, retry, cache, and routing inputs are unchanged.
    - Profile-preserved names and titles are approved only when that profile
      was applied. Canonical common acronyms remain profile-independent.
      Spotify, KTV, strong source IDs, dotted acronyms, URLs, email addresses,
      and bare domains are recognized without trusting arbitrary ALL-CAPS
      English.
    - Span boundaries exclude trailing ASCII sentence punctuation and handle
      structured Latin terms followed directly by Korean particles.
    - Runtime analysis aggregates the new classifications globally and per
      run while remaining compatible with older events that lack the field.
    - Validation: targeted 248 passed / 159 subtests; translator core 351
      passed / 166 subtests; full suite 960 passed / 4 skipped / 212 subtests;
      frozen replay 750 cases / 0 divergence; `git diff --check` passed.
    - Independent post-implementation review: PASS after its punctuation,
      fail-closed ALL-CAPS, structured-term, and attached-particle findings
      were fixed and covered by regression tests.

- [x] **T05 - Narrow selective translation retry**
  - Retry only defensible failures: meta/refusal output, empty output for a
    clearly translatable complete source, a long coherent source reduced to
    placeholder text such as `無內容`, provable amount mismatch, or substantial
    non-whitelisted Hangul leakage.
  - At most one retry per sentence.
  - Preserve the Japanese-shadow contract: `target_has_japanese` remains
    record-only in shadow even when another bad-output flag is also present.
  - Runtime gate: normal sentences make no extra call and the overall retry
    rate should remain below roughly 2%.
  - Implemented 2026-07-25:
    - Generic severity, high-Latin, low-CJK, and repetition signals no longer
      trigger a second opinion. Strict meta/refusal forms, coherent long-source
      placeholder reductions, and narrowly proven amount mismatches do.
      Empty provider output remains handled by the existing fallback chain.
    - Placeholder retry requires at least 24 compact source characters,
      12 Hangul syllables, 35% Hangul, and 0.65 distinct-bigram ratio.
      Repeated/noisy pseudo-speech therefore remains a correct abstention.
    - Amount retry uses independently parsed narrow proof shapes and compares
      only their captured values. Mixed amounts and inner substrings such as
      `백만 5천` cannot borrow proof from an unrelated amount.
    - T04 runtime evidence showed that unlisted Hangul commonly represents
      contract-correct Korean names and quoted terms, so the proposed active
      Hangul retry was rejected as unsafe and remains record-only.
    - A candidate must resolve the selective trigger, contain no Japanese or
      new selective defect, preserve source/original-approved profile/common
      terms, and satisfy the trigger-specific severity gate.
    - Only an engine not already present in the sentence attempt chain can be
      called, so quality retry cannot re-call a timed-out fallback engine.
      Japanese shadow remains absolute record-only for composite cases.
    - `analyze_runtime_events.py` now reports quality-retry event rate,
      applied count, triggers, and terminal reasons globally and per run.
    - Historical runtime gate:
      `scratch/analysis/t05_selective_retry_gate_20260725.json`; 25,131
      eligible live API outputs, 18 selective triggers (0.072%), 41 including
      Japanese shadow (0.163%), maximum daily upper bound 1.176%, and the
      latest 719-event runtime projects zero retries versus two legacy calls.
    - Validation: targeted 285 passed / 176 subtests; translator core 363
      passed / 176 subtests; full suite 972 passed / 4 skipped / 222 subtests;
      frozen replay 750 cases / 0 divergence; `git diff --check` passed.
    - Independent post-implementation review: PASS after its coherence,
      Hangul safety, refusal grammar, candidate preservation/cleanliness, and
      amount proof/coupling/boundary findings were fixed with regressions.

- [x] **T06 - Automatic glossary-candidate report**
  - Mine high-frequency unknown Hangul spans, inconsistent renderings,
    profile-term misses, and frequently triggered corrections from runtime
    evidence.
  - Produce candidates and counts only; never mutate the glossary
    automatically.
  - Do not turn this into another large manual-labeling batch.
  - Implemented 2026-07-25:
    - Extended the existing `scripts/suggest_corrections.py` miner instead of
      adding a second harness. It now emits the same candidate inventory as
      compact Markdown and schema-versioned JSON; `post_run_quality_loop.py`
      writes `glossary_candidates.json` beside its existing artifacts.
    - Unknown Hangul candidates prefer T04's profile-aware
      `target_unexpected_hangul_spans`. Events that predate that field retain
      the conservative project-wide allowlist fallback, and the report
      exposes telemetry/fallback and source-presence counts separately.
    - Inconsistent renderings are scoped by effective profile. Explicit
      `profile_applied=false` events are treated as general/no-profile; old
      events missing the field remain legacy-assumed applied.
    - Profile-term misses use the reviewed, active-profile `fan_terms.json`
      inventory and require the fixed rendering to be absent. The ambiguous
      `URL` alias is machine-marked and excluded because it may mean an
      ordinary web address; strong `UR:L`/Korean forms still qualify.
    - Correction telemetry is aggregated once per successful event by
      stage/rule/before/after. Overlapping event paths are deduplicated so
      counts cannot be multiplied by overlapping globs.
    - Reports are candidate-only and contain examples/counts. They never
      write correction/profile/glossary data and introduce no labeling queue.
    - Latest-runtime artifact gate on `runtime_events_20260724.jsonl`: 837
      translation events / 819 successful at `min_count=2`; zero unknown
      Hangul, inconsistent-rendering, or profile-term-miss candidates and one
      repeated correction (`name:랑코`, two events). The real post-run loop
      produced parseable runtime JSON, Markdown suggestions, and glossary
      candidate JSON.
    - Historical compatibility gate scanned 28,820 translation events in
      3.2 seconds and produced bounded candidate inventories without changing
      source data.
    - Validation: relevant targeted tests 40 passed; translator core 363
      passed / 176 subtests; full suite 985 passed / 4 skipped / 222 subtests;
      frozen replay 750 cases / 0 divergence; `git diff --check` passed.
    - Independent post-implementation review initially found disabled-profile
      attribution, ambiguous `URL`, duplicate-path, and JSON-artifact gaps.
      After fixes and regressions, final re-review PASS with every finding
      closed and no new blocker or major issue.

- [x] **T07 - Sentence-hold shadow telemetry**
  - Record when a sentence ends with an unfinished Korean connector, particle,
    unclosed delimiter, or truncated lexical fragment.
  - Estimate whether waiting 300-500 ms or one next STT chunk would have merged
    useful content.
  - Do not alter live sentence timing in this card.
  - Implemented 2026-07-25:
    - Added pure, record-only tail classification for unfinished Korean
      connectors/particles, unclosed paired delimiters, and possible forced
      lexical truncation. Short `사과`/`최고`-shaped words and ASCII
      apostrophes in contractions are excluded from structural evidence.
    - The splitter records a candidate at the existing cut decision and
      observes exactly the first subsequent STT chunk. Candidate disposition
      distinguishes already-buffered cuts from actually emitted cuts; outcome
      telemetry records next-chunk delay and the 300/500 ms windows.
    - Shadow outcome calculation admits the observed token to the buffer and
      completes the existing cut/merge/queue decision before persisting the
      extra event. No sleep, wait, hold, merge, routing, or subtitle behavior
      was added. Candidate writes remain synchronous and can add minor disk
      overhead before a later loop iteration; this is explicitly a telemetry
      cost, not an active hold.
    - Connector/particle evidence is deliberately weak
      `raw_continuation_heuristic` only. It cannot prove semantic continuity.
      The strict `useful_merge_heuristic` used for a future T08 gate accepts
      only meaningful next text that observably closes an unclosed delimiter;
      lexical fragments never become useful from text-only guessing.
    - `analyze_runtime_events.py` pairs events by `(run_id, shadow_id)`, admits
      only one matched outcome into rates, and exposes unresolved, orphan, and
      duplicate records. It reports actionable emitted opportunities
      separately from cuts the current pending-incomplete buffer already held.
    - Timeout and stop regressions prove one candidate receives exactly one
      terminal outcome and is not recreated when the buffered sentence emits.
      Pause/supersede/merge-skip paths remain covered by lifecycle structure
      and the existing splitter behavior suite.
    - Historical classifier-only compatibility probe: the latest 837
      translation events yielded 30 connector, 27 particle, and 34 possible
      lexical-tail signals; all 28,820 historical translations scanned in
      2.78 seconds. These old logs predate outcome telemetry, so they cannot
      establish a 300/500 ms benefit.
    - Validation: targeted 51 passed / 23 subtests; translator core 363 passed
      / 176 subtests; full suite 996 passed / 4 skipped / 222 subtests; frozen
      replay 750 cases / 0 divergence; `git diff --check` passed.
    - Independent post-implementation review: PASS after its cross-run
      pairing/rate-integrity and over-broad useful-merge findings were fixed.
      Remaining minor is synchronous candidate-event disk overhead.
  - A real post-T07 runtime is still required before T08. Do not infer hold
    benefit from the historical classifier-only probe and do not implement
    adaptive timing until `actionable_emitted.useful_within_300ms/500ms`
    provides sufficient evidence.

- [x] **T08 - Adaptive sentence hold — deferred pending runtime evidence**
  - Implement only if T07 shows useful merge coverage.
  - Wait for at most one next chunk or a bounded 300-500 ms.
  - Hard maximums always win, and a kill switch is required.
  - Runtime gate: compare forced/incomplete rate, merge rate, and user-visible
    delay to the baseline.
  - Deferred 2026-07-25:
    - The repository contains 41 `runtime_events_*.jsonl` files and zero
      `sentence_hold_shadow` events. The newest file is
      `runtime_events_20260724.jsonl`; its live runs predate T07 commit
      `4e46368`.
    - The existing runtime analyzer reports zero actionable emitted
      candidates and therefore zero measured `useful_within_300ms` or
      `useful_within_500ms` coverage. Those zeros mean "not observed", not
      evidence that a hold is ineffective.
    - Adding a live wait without this outcome evidence would violate T08's
      implementation gate and introduce unquantified user-visible delay with
      no measured benefit to offset it. No timing, merge, or configuration
      behavior was changed.
    - Reconsider only after at least 50 matched actionable emitted outcomes
      across at least two post-T07 live runs. Before implementation, record a
      delay-versus-strict-useful-merge acceptance threshold from that evidence
      and keep it fixed for the runtime comparison; a zero or undersized
      sample does not pass.
    - Any future implementation must still keep the bounded
      one-chunk/300-500 ms limit, hard-maximum precedence, kill switch, and
      before/after runtime gate.

- [x] **T09 - Selective secondary STT replay — offline complete, live no-go**
  - Start offline with existing audio dumps.
  - Candidate triggers include low log probability, abnormal compression,
    forced blobs, or a high-similarity glossary miss.
  - A rescue must use the original audio evidence. Do not substitute a
    text-only LLM normalizer that guesses missing speech.
  - Do not connect it to live runtime unless offline evidence shows a material
    benefit.
  - Completed offline 2026-07-25:
    - Hardened the existing `scout_sensevoice_historical.py` instead of adding
      a parallel harness. Selection now starts from WAV-backed STT events and
      records low-logprob, compression-rejection, and forced-cut triggers.
      Glossary-similarity candidates remain T10's responsibility.
    - Forced sentence events are independently joined to every source
      utterance, including cases with no successful or comparison-safe
      translation. The historical inventory contains all 6,613/6,613
      WAV-backed sentence-forced utterances.
    - Groq/SenseVoice disagreement is computed only when a successful
      translation is explicitly single-source, has an explicitly present and
      empty evidence-attribution list, and its raw source length matches the
      STT event's recorded text length. Missing attribution, evidence-bearing
      or multi-source events, and text-length mismatches remain replayable but
      cannot become comparison rows.
    - Replayed 24 bounded high-risk WAVs through local SenseVoice: 24 outputs,
      11 structurally comparable pairs, 10 disagreements >= 0.5, inference
      p50 547 ms / max 875 ms. Exact heard-source ground truth remains zero,
      so measured rescue and false-correction rates remain null.
    - The offline artifact therefore records live `no-go`. Non-empty output
      and engine disagreement are candidate signals, not correctness, and no
      live STT/resolver/config path was changed.
    - Validation: targeted 11 passed; translator core 363 passed / 176
      subtests; full suite 1001 passed / 4 skipped / 222 subtests; frozen
      replay 750 cases / 0 divergence; `git diff --check` passed.
    - Independent review initially found unsafe legacy evidence attribution
      and 879 missing forced candidates. After fixes, re-review PASS with
      6,613/6,613 forced candidates covered and no remaining finding.

- [x] **T10 - Conservative fuzzy source normalization — record-only shadow**
  - Restrict candidates to the active profile glossary.
  - Require a strong, unique match; leave the source unchanged when candidates
    are close or ambiguous.
  - Begin in record-only shadow mode.
  - Implemented 2026-07-25:
    - Translation events now carry a fail-closed `source_fuzzy_shadow`
      diagnostic. It is always `mode=record_only`, always `applied=false`, and
      the original source remains the only text sent to the translator.
    - Production canonical targets are limited to the intersection of the
      active profile STT glossary and reviewed source-normalization/fan-term
      canonicals. Generic glossary entries and aliases cannot become fuzzy
      targets.
    - Exact-length Hangul tokens require at most one NFD-jamo edit, normalized
      distance at most 0.20, and at least 0.10 separation from the runner-up.
      Close candidates remain observable as ambiguous but propose no change.
    - Reviewed forms from the active profile glossary, source-normalization
      keys, fan-term aliases, and active/shared name-rendering source aliases
      are exclusion-only. They cannot be reinterpreted as fuzzy misses or
      promoted as canonical targets. Korean vocative endings are also
      excluded from final-syllable name rewrites.
    - `LIVE_TRANSLATE_SOURCE_FUZZY_SHADOW` is the kill switch. Any diagnostic
      exception returns a bounded `diagnostic_error` record and cannot block
      translation, including malformed profile input.
    - Runtime analysis reports coverage, eligibility, unique/ambiguous
      candidates, would-change/applied counts, errors, profiles, reasons,
      pairs, and bounded samples globally and per run.
    - Historical blast-radius replay scanned 27,936 successful translations:
      51 candidate events, 58 unique proposals, zero ambiguous rows, and zero
      errors in about 8.9 seconds. An independent alias inventory found zero
      reviewed aliases remaining among fuzzy proposals. Warm no-candidate
      overhead measured about 0.153 ms/event; no real post-T10 live run exists
      yet, so activation remains prohibited and the card ends in shadow.
    - Validation: targeted 239 passed / 169 subtests; translator core 364
      passed / 176 subtests; full suite 1,011 passed / 4 skipped / 222
      subtests; frozen replay 750 cases / 0 divergence; `git diff --check`
      passed.
    - Independent post-implementation review initially found incomplete
      reviewed-alias exclusion and an exception fallback that could rethrow.
      After corrections and regressions, final re-review PASS with no blocker
      or major issue.

- [x] **T11 - Hedged fallback — deferred pending post-T03 live evidence**
  - Consider only after T01-T03 and only if translation p99 remains too high.
  - Start OpenRouter in parallel only after NVIDIA exceeds a measured
    2.5-3-second hedge threshold; select the first valid result.
  - Preserve trace attribution, cancellation safety, and token/cost telemetry.
  - Deferred 2026-07-25:
    - Every available live run predates T01-T03. The latest baseline run has
      261 translation events / 254 successes and success latency p50 1,047 ms,
      p95 2,016 ms, and p99 3,656 ms. Its 225 NVIDIA attempts have p50
      1,031 ms, p95 1,985 ms, and p99 3,656 ms.
    - Only four NVIDIA attempts crossed 2.5 seconds (all four also crossed
      3 seconds): two valid slow successes at 3,125/3,656 ms and two empty
      provider failures at about 5.1 seconds. One rejected-output fallback
      occurred earlier at 1,469 ms and is not a latency-hedge candidate.
    - T03's OpenRouter artifact proves 40/40 independent capsule requests with
      p50 898 ms / p95 2,344 ms / max 3,547 ms and sample cost US$0.00339.
      It does not measure post-T03 live routing, overlapping winner/loser
      results, duplicate paid calls, or hedge attribution.
    - The current synchronous `urllib` request cannot cancel an in-flight
      loser. Implementing now would therefore add unmeasured late results,
      duplicate cost, and concurrent telemetry semantics on evidence from an
      obsolete fallback path. No live routing code or configuration changed.
    - Reconsider only after at least two representative post-T03 live runs.
      Before implementation, freeze a gate using the observed NVIDIA
      >=2.5/3-second population, projected first-valid p99 improvement,
      duplicate OpenRouter call/cost rate, and a defined late-loser telemetry
      policy. A sparse or zero hedge population does not pass.
    - Independent evidence-gate review: PASS for deferral with no blocker or
      major issue; it independently verified the run SHAs, latency counts,
      pre-T03 provenance, benchmark limitation, and cancellation constraint.

- [x] **T12 - Low-cost activity context — explicit opt-in implemented**
  - Prefer explicit user activity, platform/category metadata, browser/media
    title, or repeated known game terms.
  - Add at most one short background capsule.
  - Do not re-enable broad full-screen vision capture that can ingest unrelated
    windows or local workspace content.
  - Implemented 2026-07-25:
    - The existing `current_activity` setting is now exposed in the dashboard
      as an explicit optional input and preserved by the TypeScript/Rust config
      schema. Dashboard values are one line and at most 80 characters; changes
      retain the existing restart-required configuration semantics.
    - One shared normalizer supplies NVIDIA's full prompt, Groq/OpenRouter
      compact prompts, DeepL context/cache identity, STT scene hot terms, and
      runtime metadata. It applies NFKC, collapses whitespace, caps at 80
      characters, rejects every Unicode `C*` category, and fails closed on
      instruction-like metadata. Rust independently rejects controls and the
      complete Unicode `Cf` range at the dashboard boundary.
    - Each LLM request receives at most one labeled activity capsule. Full and
      compact prompts keep a final output-only rule after that capsule.
      OpenRouter with empty activity remains byte-for-byte identical to T03's
      benchmarked capsule/hash; all empty-activity paths remain opt-in/no-op.
    - DeepL uses the same normalized value, so equivalent whitespace does not
      rotate its cache signature. Translation runtime events store that same
      bounded value, and STT activity vocabulary cannot see a different raw
      string.
    - The existing vision `scene_context` remains disabled and unchanged. No
      screen, browser, OCR, local workspace, platform, or media-title content
      was newly collected.
    - Validation: Python targeted 352 passed / 176 subtests; translator core
      366 passed / 176 subtests; full suite 1,018 passed / 4 skipped / 222
      subtests; frozen replay 750 cases / 0 divergence; frontend 55 tests plus
      `vue-tsc`; Rust 41 tests; `git diff --check` passed.
    - Independent review initially found Groq's final-rule ordering and
      Unicode-format/instruction sanitizer bypasses. After corrections and
      regressions, final re-review PASS with no blocker or major issue.
    - A real run with non-empty explicit activity has not occurred. Do not
      claim live quality improvement until such a run is analyzed. This does
      not activate background collection or alter the default empty path.

- [x] **T13-A - Safe automatic activity resolver — record-only implemented**
  - Implemented 2026-07-25 as a shadow-only extension of T12. Automatic
    activity can be observed and measured but cannot publish, modify
    `cfg.translation.current_activity`, enter a translation prompt/cache, or
    activate STT hot terms.
  - The resolver admits exactly one visible Chrome HWND whose active title
    matches a configured platform marker. Zero, multiple, wrong-tab,
    PID/class/platform identity change, and detectable HWND reuse all fail
    closed. Capture uses a `WindowCaptureBackend` abstraction with
    `PrintWindow` as the current implementation; failure never falls back to
    full-screen capture, desktop bbox capture, or an arbitrary top-most
    browser.
  - Browser chrome, window edges, and the likely side-chat region are removed
    before quality checks, fingerprinting, or vision. Black/low-variance,
    unavailable, exact-repeat, and near-identical frames cannot increase
    consensus. Frames and full titles remain in memory only and are never
    persisted.
  - Confirmation requires two distinct evidence items for the same small
    canonical activity ID: either a strict canonical title plus a player-only
    vision frame, or two sufficiently different vision frames from different
    analysis cycles. Repeated title polls, identical fingerprints, small
    cursor-like changes, stale evidence, and evidence from another window
    generation do not count.
  - `resolver_generation`, `window_generation`, and `effective_generation`
    separate lifecycle/config, HWND identity, and manual/publication changes.
    Late resolver/window/pause/stop results are discarded. A result whose only
    mismatch is `effective_generation` may still update shadow evidence but
    remains unpublished. Pause clears confirmed automatic state so resume
    requires fresh evidence.
  - TTL and freshness decisions use `time.monotonic()`; UTC is retained only
    as human-readable confirmation telemetry. `vision_unknown` lowers
    confidence without refreshing freshness, capture-unavailable keeps the
    current deadline, and invalid/wrong-tab sources use a shorter,
    non-extending invalid-window TTL.
  - Manual activity is authoritative. While manual activity is non-empty the
    resolver continues shadow calculation; clearing manual state leaves a
    still-fresh automatic snapshot available only as a future publication
    candidate. T13-A's fixed record-only policy still returns no automatic
    effective activity.
  - Every translation request now binds one immutable manual
    `ActivitySnapshot` via `ContextVar`. Main prompt construction,
    engine-specific compact prompts/DeepL context, effective prompt/cache
    signature, API attempts/retries, fallback probes, and runtime metadata
    therefore cannot split across two manual activity values during an
    in-flight change. Existing manual display text and prompt semantics remain
    unchanged.
  - `activity_shadow` telemetry records opaque request IDs, generations,
    platform/title booleans, capture/quality status, bounded canonical
    candidate IDs, distinct/reused evidence, latency, provider/model, manual
    override state, and precise discard reasons. It explicitly records
    `published=false`, `translation_context_applied=false`, and
    `stt_terms_applied=false`; no full title, ordinary title hash, frame,
    fingerprint, evidence key, or raw vision text is emitted.
  - `analyze_runtime_events.py` now summarizes shadow request, confirmation,
    duplicate/distinct evidence, manual-override, publication-violation,
    status/reason/activity, and latency counts globally and per translation
    run. The translator-core harness includes the activity snapshot tests.
  - Non-goals remain: no STT activity evidence, no automatic STT terms, no
    translation-prompt semantic change, no large taxonomy, no selection among
    multiple candidate windows, no saved images/titles/raw vision text, and no
    silent vision-provider/model fallback.
  - Validation: targeted scene/activity/analyzer tests passed; translator core
    373 passed / 176 subtests; full suite 1,034 passed / 4 skipped / 222
    subtests; frozen replay 750 cases / 0 divergence; `git diff --check`
    passed. A local Windows smoke resolved one platform HWND and returned
    `capture_status=ok`, `frame_quality=ok`, `content_crop=true` without
    persisting bytes or calling a vision API.
  - Historical note: this paragraph originally ended before a provider-backed
    run. T13-A subsequently completed Groq-backed canonical confirmation and
    bounded timeout observation; T13-B below owns the later publication
    activation decision. Do not use the earlier local-only smoke as proof of
    translation quality improvement.
  - Independent post-implementation review initially found that completion
    validation occurred after the external call, did not re-enumerate for a
    second candidate/exact-title change, and allowed duplicate evidence to
    refresh TTL. After adding full post-capture, pre-provider, and
    post-provider validation plus duplicate-refresh guards and regressions,
    focused re-review returned PASS with all three findings closed.

- [x] **T13-B - Automatic activity publication — implemented and observed**
  - This is the intended successor to T13-A and the feature that addresses the
    original product problem: manual activity entry is too inconvenient when a
    livestream is simply left playing. T13-A only measures the resolver and
    deliberately cannot improve translation yet; T13-B is the separate
    activation decision and publication path.
  - Do not implement T13-B merely because T13-A unit tests passed. First run
    the current disabled-by-default resolver against a representative real
    livestream with its explicitly configured vision provider/model, then
    analyze the resulting `activity_shadow` events. Confirm provider
    availability and cost before enabling the run; do not silently substitute
    another vision model.
  - The runtime gate must establish, from existing analyzer output and direct
    run inspection, that the resolver locks only the intended dedicated
    livestream HWND, fails closed for zero/multiple/wrong-tab candidates,
    rejects black/stale/duplicate frames, reaches consensus from genuinely
    distinct evidence, expires correctly under unknown versus invalid-window
    conditions, and produces no publication/context/STT-term violations.
    Record latency, vision-call count/cost, confirmation/expiry churn, late
    discard reasons, and the bounded candidate activity IDs. Do not create a
    bulk manual-labeling exercise.
  - If that evidence passes, freeze a T13-B proposal before implementation.
    The minimum publication contract is:
    - non-empty manual activity always wins;
    - the automatic resolver continues low-frequency shadow work during a
      manual override;
    - clearing manual activity immediately restores the latest still-fresh,
      already-confirmed automatic snapshot rather than forcing consensus again;
    - no fresh confirmed automatic snapshot means empty activity, never a
      guessed fallback;
    - resolver/window generation mismatches discard the whole late result,
      while an effective-generation-only mismatch may update automatic shadow
      state but cannot change the in-flight translation;
    - each translation keeps one immutable effective `ActivitySnapshot` across
      prompt construction, compact/full engine variants, cache identity,
      retries/fallbacks, and runtime metadata;
    - cache identity is `activity_id + activity_context_schema_version`, not
      display label, confidence, source, confirmation time, or snapshot
      version.
  - Publication must have its own default-off kill switch and telemetry. Keep
    the existing safe HWND resolver, window-only capture abstraction,
    title/frame privacy boundary, distinct-frame consensus, monotonic TTLs,
    bounded canonical taxonomy, and manual precedence unchanged. No full-screen
    or bbox fallback, raw title/frame/vision logging, multiple-window
    auto-selection, ordinary title hash, or silent provider fallback.
  - Automatic STT hot-term activation is not implicitly authorized by
    translation-context publication. Decide and gate it separately after
    T13-B translation-only evidence unless a reviewed proposal demonstrates
    that coupling is safe. Until then automatic activity may affect neither
    STT terms nor any other upstream recognition behavior.
  - Required race regressions include: activity changes during one translation;
    tab/window/PID/class changes during capture or provider calls; HWND numeric
    reuse; pause/stop with an in-flight request; manual override entered or
    cleared during a request; and two simultaneous platform windows. Existing
    T13-A tests are the baseline and must remain green.
  - Implemented 2026-07-26:
    - A thread-safe publication store enforces manual > fresh automatic >
      empty precedence without mutating the manual config field. Every
      translation binds one immutable effective snapshot, and cache identity
      remains canonical activity ID plus activity schema version.
    - Publication has a separate switch and transition telemetry. Window
      generation changes, expiry, pause/stop, manual changes, and provider
      races clear or discard state according to the contract above. Automatic
      activity remains translation-only and never activates STT terms.
    - The bounded taxonomy now includes League of Legends. Unicode-aware title
      boundaries prevent short `lol` from matching inside unrelated Latin or
      non-Latin words.
    - Live run `20260726T111410Z-156980` confirmed
      `league_of_legends`, published it to hundreds of translations, exercised
      expiry and reconfirmation, and recorded zero publication/STT safety
      violations. This proves activation/lifecycle behavior, not translation
      quality improvement.
    - The same run exposed the next bottleneck: 8 of the first 18 Groq vision
      requests timed out at the 20-second boundary despite no recorded 429 or
      TPM exhaustion. T13-P below isolates that transport concern from
      activity identity and publication.

- [x] **T13-P - Explicit provider-neutral scene vision routing**
  - Implemented 2026-07-26 as a transport-only successor to T13-B. It does not
    change capture, canonicalization, consensus, publication, translation
    prompts, cache identity, or STT behavior.
  - `modules/scene_vision.py` owns an immutable provider registry, explicit
    provider/model routes, one-attempt Groq and OpenRouter adapters, bounded
    diagnostics, and the route runner. Provider/model/key/timeout values are
    captured when the resolver starts and cannot split during one request.
  - Fallback occurs only after timeout, connection failure, rate limit, HTTP
    5xx, parse failure, or genuinely empty provider content. Auth/payment,
    general HTTP 4xx, configuration errors, valid `unknown`, and bounded
    noncanonical responses fail closed without advancing.
  - Configuration validates provider/model pairs, unique routes, a maximum of
    three routes, model-ID shape/length, positive finite timeout, and zero SDK
    retries. Startup fails if any explicitly configured route lacks its key;
    providers are never inferred from whichever credentials happen to exist.
  - Groq remains the primary route. The initial implementation kept the paid
    fallback tuple empty; the owner explicitly activated OpenRouter
    `qwen/qwen3-vl-32b-instruct` on 2026-07-27 for the runtime gate.
  - `activity_shadow` retains backward-compatible top-level provider fields and
    adds a bounded attempt chain containing only provider, model, outcome,
    retryability, error/status, latency, tokens, rate-limit data, and cost.
    The analyzer aggregates attempt usage/cost without double-counting the
    selected top-level result and remains compatible with old logs.
    - Validation before independent review: targeted 137 passed / 5 subtests;
      translator core 383 passed / 181 subtests; full suite 1,080 passed /
      4 skipped / 227 subtests; frozen replay 750 cases / 0 divergence;
      legacy runtime analyzer replay and `git diff --check` passed.
    - Independent review returned REVISE with no blocker and one major finding:
      malformed OpenRouter response metadata could bypass the retryable parse
      boundary. Response schema validation and a fallback regression test now
      cover that path. The required post-review affected rerun passed:
      138 tests / 5 subtests.
  - Runtime fallback activation remains a separate explicit owner decision.
    When enabled, the gate must show one retryable Groq failure followed by a
    correctly attributed OpenRouter result, plus a valid `unknown` case that
    makes no paid fallback call. Do not claim production fallback reliability
    from mocked tests.
  - Owner activation began 2026-07-27 in live run
    `20260727T125008Z-50076`. The resolver loaded the explicit two-route chain,
    and its first valid Groq `unknown` stopped after one attempt with no paid
    fallback call.
  - The fallback runtime gate completed in live run
    `20260728T095335Z-153396`: a Groq vision request timed out after 20,281 ms,
    was classified retryable, and OpenRouter
    `qwen/qwen3-vl-32b-instruct` succeeded on the second attributed attempt in
    813 ms for US$0.000057616. The late result was correctly discarded after
    a window-generation change. Separate valid `unknown` results stopped after
    Groq without a paid fallback call. This proves routing/fail-closed
    behavior, not scene-classification quality.

- [x] **T14 - Provider-neutral translation reliability foundation**
  - Evidence: live run `20260727T125642Z-55768` completed 504 translation
    events, but OpenRouter's configured 8-second socket timeout produced an
    11,547 ms parse-failure attempt and a 12,703 ms fallback-chain wall time.
    Translation p95/p99/max were 3,969/6,735/19,203 ms. The anthropic
    OpenRouter-primary chain reported `circuit_breaker_enabled=false`, so one
    provider failure persisted DeepL traffic until a background probe; four
    DeepL outputs included lower-quality profile-name transliterations.
  - Scope:
    - make the existing provider-failure circuit policy apply to every live
      provider/model route, not only the NVIDIA backend;
    - expose stable `provider:model` route identity in translation attempts,
      selected translation events, and circuit/probe events;
    - enforce one end-to-end live API budget plus each adapter's smaller route
      timeout using a caller-side wall deadline;
    - bound non-cancellable late synchronous requests per route so a provider
      outage cannot create an unbounded thread/request backlog;
    - preserve ordered output, failure-scope rules, background recovery probes,
      prompt/correction policy, and the paid OpenRouter quality primary.
  - Non-goals: no hedged duplicate requests, no free-provider activation, no
    model-quality ranking change, no extra translation workers, and no
    out-of-order subtitle emission.
  - Test gate: route timeout must release the caller and reach fallback within
    the sentence budget; sentence-budget exhaustion must stop before a lower
    route; anthropic/OpenRouter provider failure must open the same circuit as
    NVIDIA; content rejection and general 4xx must remain soft-only; route
    identity must distinguish models on the same provider.
  - Runtime gate: a post-implementation live run must show bounded deadline
    telemetry and attributable circuit transitions without permanent sequence
    stalls. Mocked tests prove the isolation contract, not provider latency or
    translation-quality improvement.
  - Implemented 2026-07-28:
    - Circuit configuration now lives under `cfg.translation` and applies to
      every live route. Content/provider failure scopes and contiguous
      fallback advancement are unchanged.
    - Translation attempts and selected/fallback/probe events expose stable
      `provider:model` route identity. Quality retry excludes attempted routes,
      so future same-provider/different-model routes remain distinguishable.
    - Live API work has a 10-second sentence budget; each adapter's timeout is
      the smaller route cap. A daemon call boundary releases the subtitle
      worker at the wall deadline, copies the bound activity context, restores
      diagnostics/token attribution, and bounds non-cancellable late calls to
      two per route.
    - The active-route Qwen prompt selector now follows the configured backend
      and first available chain route instead of assuming NVIDIA. This was
      required after the owner-approved OpenRouter-primary cutover.
    - Validation before review: targeted 296 passed / 189 subtests; translator
      core 390 passed / 189 subtests; full suite 1,089 passed / 4 skipped /
      235 subtests; `git diff --check` passed.
    - Independent review returned REVISE with one major finding: Claude and
      Google Translate had internal timeouts but no route timeout surface or
      bounded diagnostics, so their provider failures could remain `unknown`.
      One minor finding noted that probe events dropped deadline diagnostics.
      Both were fixed with real-adapter regressions and persisted probe
      attribution.
    - Required post-review reruns passed: affected targeted 301 tests / 193
      subtests; translator core 395 tests / 193 subtests; engine/prompt/activity
      blast radius 65 tests / 7 subtests; full suite 1,094 passed / 4 skipped /
      239 subtests; `git diff --check` passed.
  - Post-implementation live run `20260728T095335Z-153396` completed the
    normal-path runtime gate. A consistent snapshot at 2026-07-28T10:43Z
    contained 276 translations: 275 succeeded and one short `ISTP` input was
    rejected by OpenRouter, DeepL, and Groq as content output, without opening
    a provider circuit. OpenRouter API wall latency was p50 828 ms / p95
    1,750 ms / p99 2,485 ms / max 6,750 ms; queue wait was p95 0 ms / max
    16 ms; predecessor stall was p95 63 ms / max 469 ms. Sequence output had
    no observed permanent stall. The 275 OpenRouter cost observations totaled
    US$0.02396605.
  - This live sample produced zero translation-provider timeouts, deadline
    exhaustions, or circuit transitions. It therefore validates stable route
    operation and absence of false circuit opens, while the deadline,
    saturation, and provider-failure transition contracts remain supported by
    the deterministic fault tests above rather than a naturally occurring
    live outage. Do not cite the separate scene-vision Groq timeout as T14
    translation circuit evidence.

- [x] **T15 - Open-set model-derived activity identification — complete**
  - User decision (2026-07-28): the vision model, not a project-maintained
    enumeration of every possible game or stream activity, owns recognition.
    Known aliases may stabilize names but must not remain an admission list.
  - Observed evidence:
    - Completed live run `20260728T095335Z-153396` made 23 valid scene-vision
      requests; all 23 provider calls succeeded, 22 ended as `vision_unknown`,
      and 543 translations contained zero automatic activity applications,
      confirmations, or publications.
    - `modules/scene_context.py` currently asks for exactly one item from five
      named games or `unknown`, then `canonical_activity()` rejects every
      result outside `_ACTIVITY_REGISTRY`.
    - `ActivityPublicationStore.replace()` independently rejects every
      automatic activity whose ID/display label is not in its closed canonical
      registry.
    - These code constraints prove that an out-of-registry model result cannot
      publish. They do not prove that all eight live `unknown` results were
      caused by the registry; crop quality and model recognition remain
      alternative explanations for the runtime outcome.
  - Claims to implement:
    - **C15-1 (user decision/code):** ask the model for one exact JSON object
      containing a bounded activity `kind` and concise stable `label` derived
      from the player pixels. Use the official short title when a specific
      game/application is identifiable and a fixed generic label for broad
      kinds such as chatting or singing. A fixed `unknown` object remains the
      required abstention when evidence is weak. The small kind vocabulary is
      a schema/safety boundary, not an enumeration of games or activities.
    - **C15-2 (security):** accept only the exact JSON schema with no extra
      keys, prose, Markdown fences, role prefixes, or salvaged first line.
      Labels remain bounded by the configured maximum, NFKC-normalized, free
      of Unicode control/format characters, URLs/emails/handles and
      instruction-like metadata, and restricted to a label-shaped Unicode
      character set. Unsafe or malformed successful model output fails closed
      and does not force a paid route fallback.
    - **C15-3 (identity/cache):** known aliases retain their existing stable
      canonical IDs and display labels. Every other accepted label receives a
      deterministic, bounded, collision-resistant automatic ID derived from
      its kind plus complete normalized case-folded label. Use a cryptographic
      digest, not a lossy slug, and keep the ID opaque in telemetry. The
      publication store recomputes and validates the kind/ID/label tuple before
      accepting it.
    - **C15-4 (consensus):** arbitrary activities require two genuinely
      distinct vision frames with the same normalized identity. Existing
      title evidence may accelerate only already-reviewed known aliases; it
      must not turn arbitrary browser-title text into activity context.
      `unknown`, malformed output, or a different identity resets pending
      open-set consensus instead of allowing `A -> unknown -> A` to confirm.
      Bound the number of distinct open-set identities admitted per window
      generation; exceeding the cap fails closed until the generation changes.
    - **C15-5 (unchanged safety):** manual activity remains authoritative.
      Window/PID/class/title generations, pause/stop, distinct-frame checks,
      monotonic TTLs, translation-request snapshots, provider routing, raw
      title/frame privacy, and automatic-STT isolation remain unchanged.
    - **C15-6 (observability):** reuse the existing bounded
      `candidate_activity_id`, publication, translation activity, provider
      attempt, latency, token, rate-limit, and cost telemetry. Do not log raw
      model responses, frames, fingerprints, evidence keys, or full browser
      titles. Persist only accepted bounded labels, never rejected/raw
      responses, and add the non-freeform activity kind where needed for
      attribution. Do not add a parallel analyzer unless the existing analyzer
      cannot express the runtime gate.
  - Scope:
    - `modules/activity_context.py`: automatic open-set kind/identity and
      publication validation;
    - `modules/scene_context.py`: open-set prompt, strict response parsing,
      per-window identity cap, and fail-closed consensus while retaining known
      title aliases;
    - `config.py`: add a separate default-off open-set publication switch,
      bound `max_activity_chars`, and bound the per-window identity cap;
    - existing activity/scene/config/analyzer tests and the owning architecture
      documentation.
  - Non-goals: no STT hot-term activation, no full-screen/bbox capture, no
    browser-title publication, no OCR text ingestion, no model/provider
    ranking change, no additional API retries, no large activity taxonomy, no
    new labeling batch, and no translation prompt-policy change beyond the
    already-authorized bounded activity capsule receiving a new activity.
  - Alternatives:
    - Keep expanding a fixed allowlist: maximally deterministic but rejected by
      the owner because activity coverage is unbounded and model replacement is
      frequent.
    - Publish raw free-form model text: widest coverage but rejected because it
      weakens prompt-injection, identity, cache, and telemetry boundaries.
    - Open-set bounded labels with deterministic identity: selected because it
      leaves semantic recognition with the model while keeping publication and
      cache behavior deterministic.
  - Assumptions and falsification:
    - The configured vision models can return a stable short label across two
      representative frames. A live run with repeated label churn or continued
      `unknown` falsifies this and must lead to prompt/model/crop diagnosis,
      not a new game allowlist.
    - Exact normalized-label consensus is sufficiently conservative. Tests
      must prove spelling/case normalization behavior and that different
      labels cannot share evidence or cache identity.
  - Test gate:
    - an out-of-registry title such as `The Finals` or a non-game activity such
      as `Chatting` can confirm from two distinct vision frames and publish;
    - unsafe, multiline, sentence-shaped, overlong, extra-key, fenced, and
      malformed responses fail closed; known aliases remain backward
      compatible;
    - kind/ID/label mismatches, slug-like collision pairs, Unicode
      normalization variants, and non-Latin labels are bounded and
      deterministic;
    - `A -> unknown -> A`, `A -> B -> A`, and per-generation identity-cap
      exhaustion cannot combine evidence or create unbounded cache identities;
    - duplicate frames, label changes, window-generation changes, manual
      override changes, pause/stop, TTL expiry, and provider fallback retain
      their existing behavior;
    - targeted scene/activity/config/analyzer tests, translator core, full
      pytest, frozen replay, and `git diff --check` pass.
  - Runtime gate: first run with the new open-set publication switch off. A
    representative activity not admitted by the old five-game registry must
    reach two-frame shadow confirmation, or the run must provide attributable
    accepted/abstained/rejected evidence for why it did not. Only after that
    record-only evidence passes may the owner-activated switch publish the
    same bounded identity to translation. The analyzer must show no
    publication/STT safety violation, no rejected/raw-data telemetry expansion,
    bounded provider attempts/cost, and correct invalidation after a real scene
    or window change. A successful publication proves lifecycle behavior, not
    translation-quality improvement.
  - Codex round-1 review (2026-07-28): **REVISE**.
    - Verified C15 closed-set evidence in both `scene_context.py` and
      `ActivityPublicationStore`; verified that provider routing treats
      nonempty model content as success and therefore must not use paid
      fallback to repair content/schema rejection.
    - **B15-1:** first-line salvage is not an adequate trust boundary for
      model-derived open-set context. Require an exact bounded JSON schema,
      explicit accepted/abstained/rejected outcomes, and no raw response
      persistence.
    - **B15-2:** a lossy slug is not a safe cache identity. Require an opaque
      digest of activity kind plus the complete normalized label, one-to-one
      store validation, and an activity-context schema-version decision.
    - **B15-3:** `A -> unknown -> A` currently combines evidence. Open-set
      abstention/rejection must reset pending consensus, and distinct
      identities admitted per window generation must be bounded.
    - **B15-4:** the existing overall publication switch predates open-set
      trust. Add a separate default-off open-set publication kill switch so a
      record-only gate can run while already-reviewed known aliases retain
      existing T13-B behavior.
    - The reviewer proposed requiring a trusted catalog match for every
      publishable label and an independent second-model verification call.
      Those are not retained blockers: the owner explicitly rejected activity
      enumeration, arbitrary bounded manual labels already enter the same
      activity capsule, and no runtime evidence yet justifies an extra paid
      verification call. The stricter schema/identity/consensus/activation
      boundaries above must be tested and observed before reconsidering those
      alternatives.
  - Round-2 integration response (2026-07-28):
    - B15-1 accepted: the parser contract is exact
      `{"kind":"<bounded-kind>","label":"<bounded-label>"}` with no extra keys
      or wrapper text. `kind=unknown` requires an empty label and becomes
      abstained; schema/safety failures become rejected. Neither advances the
      provider fallback chain or persists raw content.
    - B15-2 accepted: new automatic identities use a SHA-256-derived opaque
      digest of kind plus the complete normalized label. The store validates
      the tuple one-to-one, and `ACTIVITY_CONTEXT_SCHEMA_VERSION` advances for
      the new identity contract. Existing reviewed aliases keep their stable
      IDs/display labels.
    - B15-3 accepted: abstained/rejected output resets pending consensus;
      different identities already reset through `_select`, and a bounded
      per-window-generation set rejects identity churn after the configured
      cap.
    - B15-4 accepted: reviewed known aliases remain governed by the existing
      T13-B switch, while open-set publication receives a second default-off
      switch. Confirmation/telemetry may run in record-only mode without
      changing translation.
    - Claude Code was unavailable in this environment and the independent
      proposal agent did not return a second revision before the bounded wait
      ended. This integration response is not represented as Claude output.
  - Codex round-2 re-review (2026-07-28): **YES**. B15-1 through B15-4 are
    resolved in the frozen scope above. No new blocker was introduced.
  - Implemented 2026-07-28:
    - Scene vision now requests exactly one two-key JSON object and classifies
      the result as accepted, abstained, or rejected without first-line/raw
      salvage. Duplicate keys, extra keys, wrapper prose, invalid kinds,
      kind/known-label mismatches, overlong/unsafe labels, and non-string
      values fail closed without advancing the provider route.
    - Reviewed aliases keep their historical IDs. Other accepted activities
      receive an opaque SHA-256-derived ID over the complete bounded
      kind/case-folded-label tuple; the publication store recomputes the exact
      kind/ID/label contract. Activity context schema advances to v2.
    - Abstention/rejection resets pending consensus. Open-set confirmation
      still requires two distinct matching vision frames, arbitrary titles
      cannot contribute, and at most eight distinct identities are admitted
      per window generation by default.
    - `publish_open_set_activity` is a separate default-off kill switch.
      Confirmation and bounded telemetry continue in shadow while known alias
      publication retains the existing T13-B switch. Translation metadata and
      the existing analyzer now attribute non-freeform activity kind and parse
      status/rejection reason; rejected/raw labels remain absent.
  - Independent post-implementation review (2026-07-28): **REVISE**.
    - It found four contract gaps: multilingual instruction-like labels were
      not rejected; identity-cap exhaustion rejected only the immediate new
      identity instead of latching for the window generation; broad kinds
      accepted arbitrary labels instead of their fixed generic labels; and the
      analyzer could not independently flag open-set publication with its
      dedicated switch off.
    - All four findings were fixed in scope. Common Chinese/Korean/Japanese
      instruction-shaped pairs now fail closed in addition to the existing
      Unicode/English guards; broad kinds canonicalize only their fixed label;
      cap exhaustion is latched until window-generation reset; publication
      telemetry marks open-set activity and both shadow/publication summaries
      count switch violations.
  - Validation after review fixes:
    - affected targeted suite: 375 passed / 192 subtests;
    - translator core: JSON/profile/eval checks passed and 398 pytest tests /
      199 subtests passed;
    - full suite: 1,105 passed / 4 skipped / 245 subtests;
    - frozen replay: 750 cases / 0 divergence;
    - Python compile and `git diff --check` passed.
  - Record-only runtime evidence (2026-07-28):
    - The production-config run proved one successful strict parse and zero
      publication/STT violations, but the 600-second forced-refresh cadence
      did not yield a second request during the short observation.
    - After stopping that run, one sequential validation runtime used
      non-persistent scene-only cadence overrides
      (`min_call_gap_sec=20`, `refresh_interval_sec=30`,
      `change_threshold=1`) while keeping
      `publish_open_set_activity=False`. Run
      `20260728T121500Z-215944` observed 5/5 successful provider attempts and
      5/5 accepted parses, all for the reviewed `hades` alias. It recorded
      three distinct-frame observations and two duplicate-evidence
      observations, but every per-event consensus streak remained one, so
      there was no confirmation.
    - All 21 translations in that run retained `activity_source=none`; there
      were zero activity-publication events, zero shadow/publication safety
      violations, zero vision API cost, and US$0.00193033 observed translation
      cost. The runtime was stopped and no second runtime remains.
    - This validates strict-provider compatibility, duplicate/distinct-frame
      fail-closed behavior, and open-set publication isolation. It does not
      complete the novel-identity gate because the live model classified every
      sampled frame as an old reviewed alias. Keep the open-set switch off
      until a representative out-of-registry activity produces attributable
      open-set confirmation (or accepted/abstained/rejected evidence explaining
      why it cannot).
  - Long record-only runtime gate completed 2026-07-29:
    - Cross-midnight run `20260728T150713Z-222052` covered
      2026-07-28T15:07:58Z through 2026-07-28T19:08:24Z (about 4 hours).
      The two daily log segments contained 1,371 translations: 1,360 success,
      10 policy-filtered, and one failed after OpenRouter content rejection
      followed by empty DeepL/Groq results. No translation API timeout or
      sentence-level deadline exhaustion occurred.
    - Scene vision produced 63 requests / 76 route attempts. All 17 first-day
      Groq attempts succeeded. The second segment observed 11 Groq rate-limit
      errors and one Groq timeout; all 12 retryable failures succeeded through
      the explicit OpenRouter fallback. Observed scene cost was US$0.000859456.
    - Strict parsing accepted 52 responses and rejected 11: seven were
      identity-cap lockout and four were unsafe labels. Four distinct novel
      identities confirmed (three `game`, one fixed-label `chatting`), for 21
      confirmed events in total. The cap stayed locked for window generation
      2 and accepted new identities only after a real generation change.
    - Every one of the 64 `activity_shadow` events retained
      `open_set_publication_enabled=false`, `published=false`,
      `translation_context_applied=false`, and `stt_terms_applied=false`.
      All 1,371 translations retained `activity_source=none`; there were zero
      activity-publication events and zero analyzer safety violations.
    - This completes the T15 novel-identity record-only gate and demonstrates
      provider fallback continuity under real Groq rate limits/timeout. It does
      not authorize publication by itself: the next gate is an owner-activated
      controlled run of the same bounded identities with
      `publish_open_set_activity=true`, followed by verification of manual
      precedence, translation-only application, invalidation, and continued
      STT isolation.
  - Controlled publication gate completed 2026-07-29:
    - Owner-authorized run `20260728T191310Z-212500` used a non-persistent
      process override with `publish_open_set_activity=true` and accelerated
      scene cadence. The shipped/local `config.py` default remained false.
      Runtime duration was 204.493 seconds.
    - Scene vision made 10 requests / 19 route attempts. All ten responses
      passed the strict schema; nine Groq rate-limit failures fell back
      successfully to OpenRouter. The novel game identity confirmed six times.
      Observed scene cost was US$0.00064844.
    - Manual-precedence timing was exercised in-process. The novel identity
      confirmed while the bounded manual gate was active, but both resulting
      transitions retained `effective_source=manual` and exposed no automatic
      translation context. Clearing manual state published the same still-fresh
      `auto-*` identity.
    - All 28 translations succeeded: 11 before confirmation used
      `activity_source=none`, three during the manual interval used `manual`,
      and 14 after manual clear used `automatic`. Publication telemetry
      recorded six `published`, two `manual_override`, and one final
      `cleared / pipeline_stopped` transition.
    - There were zero shadow/publication analyzer violations and no STT-term
      application. Normal window close stopped the single logical runtime and
      cleared publication. Translation cost observed during the gate was
      US$0.00157102. One translation provider circuit opened during the short
      run and had not reached its probe-close interval before shutdown; all
      translations still succeeded, and the preceding four-hour gate already
      demonstrated circuit closure after two successful probes.
    - T15 is complete. The separate switch intentionally remains default-off;
      enabling model-derived activity publication is an explicit owner/runtime
      choice rather than a shipped trust-default change.

- [x] **T16 - Owner-controlled open-set activation**
  - User decision (2026-07-29): keep the model/provider replaceable and the
    scene pipeline provider-neutral; after T15's record-only and controlled
    publication gates passed, expose the dedicated publication switch through
    the existing dashboard config bridge instead of requiring a Python edit.
  - Observed evidence:
    - `config.py::_Scene.publish_open_set_activity` is a validated boolean and
      remains `False` by default. T15 runtime evidence above validates the
      enabled path without changing that shipped default.
    - `utils/config_export.py` already exports the non-secret `scene` section,
      but Rust `ConfigDto` and the TypeScript `ConfigDto` omit it, so the
      dashboard cannot render or save the switch.
    - `_DASHBOARD_OVERRIDE_FIELDS` currently has no `scene` entry. Even a
      manually edited dashboard JSON therefore cannot change the switch on the
      next dashboard-launched Python restart.
  - Claims to implement:
    - **C16-1 (user decision/code):** add only
      `scene.publish_open_set_activity` to the dashboard-editable Python
      whitelist. Accept only a JSON boolean; invalid values retain the
      `config.py` default. Do not expose provider/model, cadence, capture,
      identity-cap, overall scene enablement, or the older known-alias
      publication switch.
    - **C16-2 (restart semantics):** represent the field in the Rust and
      TypeScript DTOs and add one explicit checkbox to `ConfigPanel`. Saving
      writes the existing JSON bridge; the setting takes effect only after the
      Python pipeline is restarted through the dashboard override path.
    - **C16-3 (safety invariants):** the shipped Python default remains false;
      manual activity precedence, automatic-STT isolation, strict scene
      parsing/consensus, window invalidation, and provider routing are
      unchanged. No runtime mutation or API call occurs when the checkbox is
      toggled.
    - **C16-4 (contract evidence):** Python export/override tests, Rust DTO
      round-trip tests, and Vue component tests must prove false/true
      preservation, default-off behavior, invalid-value rejection, old JSON
      compatibility when the section or field is absent, unrelated exported
      scene-field tolerance, and emitted save state.
  - Scope:
    - `config.py` dashboard whitelist/validation only;
    - `src-tauri/src/state.rs` scene DTO and tests plus
      `src-tauri/src/handlers/config.rs` fixture/parse-contract updates;
    - `src-frontend/src/types/config.ts`,
      `src-frontend/src/components/ConfigPanel.vue`, and component tests;
    - existing Python dashboard/export contract tests;
    - owning `system.md` / `frontend-design.md` contract notes.
  - Non-goals: no change to the `_Scene` default, provider/model routes,
    capture or call cadence, consensus/identity rules, translation prompt
    behavior, STT terms, live hot reload, automatic restart, new analyzer,
    runtime API experiment, or broad dashboard exposure of scene settings.
  - Alternatives:
    - Keep editing `config.py`: smallest code surface, but it risks mixing an
      operational choice with local source changes and does not use the
      existing owner config bridge.
    - Expose all scene fields: more flexible, but unnecessarily expands the
      dashboard trust/config surface and permits changes that have not passed
      equivalent runtime gates.
    - Expose only the dedicated publication switch: selected because it maps
      exactly to the completed T15 gate and preserves all other safety bounds.
  - Assumptions and falsification:
    - The dashboard JSON bridge is the intended owner-control surface. This is
      supported by the existing Tauri launcher environment opt-in and restart
      note; an alternative live-control service would require a new promoted
      architecture task.
    - A one-field `scene` DTO is sufficient because the Python override is a
      whitelist, not a full secondary config source. Contract tests must prove
      the field survives dashboard load/save, old JSON defaults it off, extra
      exported scene fields remain ignored by the DTO, and the override reaches
      `_Scene` only when it is a boolean.
  - Test gate:
    - targeted Python dashboard/config-export tests;
    - targeted Vue ConfigPanel test;
    - Rust `cargo test --locked`, including false/true round-trip, missing
      section/field default-off, non-boolean parse rejection, and unrelated
      scene-field tolerance;
    - full frontend test/build, full Python pytest, and `git diff --check`;
    - frozen translation replay is not required because no deterministic
      translation policy/correction/name-rendering behavior changes.
  - Runtime gate: no new paid/runtime scene call is required. T15 already
    validated both switch states and the publication lifecycle. T16 is complete
    when the persisted dashboard false/true value round-trips and a restarted
    config instance applies it while the source default remains false.
  - Codex round-1 review (2026-07-29): **REVISE**.
    - B16-1 found that the Rust scope omitted the second `ConfigDto` literal in
      `src-tauri/src/handlers/config.rs` and did not explicitly require old
      JSON/non-boolean/extra-field compatibility cases.
  - Round-2 integration response (2026-07-29):
    - B16-1 accepted. The handler fixture is in scope, and the Rust contract
      gate now covers true/false round-trip, missing section/field default-off,
      non-boolean rejection, and unrelated scene-field tolerance.
    - Claude Code was unavailable in this environment, so the proposal and
      integration response were prepared by Codex and independently checked by
      a fresh read-only reviewer rather than represented as Claude output.
  - Codex round-2 re-review (2026-07-29): **YES**. B16-1 is resolved and no new
    blocker was introduced.
  - Implemented 2026-07-29:
    - The Python dashboard override whitelist accepts only the dedicated
      `scene.publish_open_set_activity` boolean. Invalid values retain the
      source default while unrelated valid overrides still apply.
    - Rust and TypeScript DTOs preserve the switch, old/missing scene JSON
      defaults it off, and unrelated exported scene fields remain ignored.
    - `ConfigPanel` exposes one explicitly labeled checkbox with restart,
      translation-only, and STT-isolation wording. Provider/model/capture/
      cadence/consensus settings remain outside the dashboard scene surface.
    - The source default remains false; no scene resolver, provider, prompt,
      STT, or live runtime behavior was changed.
  - Validation:
    - Python targeted baseline before implementation: 61 passed / 19 subtests.
    - Python targeted after implementation: 62 passed / 19 subtests.
    - Vue focused: 29 passed after one selector-only test fix; full Vue:
      57 passed; production type-check/build passed.
    - Rust: the first incremental build hit a rustc 1.95 corrupted dep-graph
      ICE. Re-running non-destructively with `CARGO_INCREMENTAL=0` passed all
      44 tests.
    - Full Python: 1,106 passed / 4 skipped / 245 subtests.
    - Frozen replay was intentionally not run because T16 changes no
      deterministic translation policy, correction, or name-rendering layer.
  - Independent post-implementation review (2026-07-29): **PASS**.
    - No critical, high, or medium findings. The reviewer verified the narrow
      boolean-only override, default-off and restart semantics, DTO backward
      compatibility, Vue behavior, unchanged scene/STT safety boundaries,
      documentation, supplied validation, and exclusion of the user's local
      `config.py` profile/comment hunk.

- [x] **T17 - Pipeline-first open-set activation default**
  - User decision (2026-07-29): the Python pipeline is the primary product
    surface and the frontend is currently unused. Features whose activation
    gates have passed should not require the frontend to become effective.
  - Evidence:
    - T15 completed a four-hour record-only gate and a controlled active gate
      with manual precedence, translation-only application, invalidation,
      provider fallback, bounded cost, and zero STT/safety violations.
    - T16 added a narrow persisted kill switch, but manual `main.py` execution
      does not apply dashboard JSON and therefore still uses the Python source
      default.
  - Claims to implement:
    - **C17-1 (user decision/runtime evidence):** change only
      `_Scene.publish_open_set_activity` to default true. Keep
      `scene.enabled` and known-alias translation publication enabled as they
      already are.
    - **C17-2 (cross-surface default contract):** Rust missing-section/field
      defaults and the Vue no-config fallback must also default true so an old
      or absent dashboard JSON cannot visually or persistently turn the newly
      approved pipeline feature off by accident. A T16-era JSON that explicitly
      contains false remains off because the current schema cannot distinguish
      an automatically exported historical value from an owner-selected kill
      switch without adding migration state.
    - **C17-3 (kill switch):** an explicitly persisted JSON boolean false must
      still override the true source default on the next dashboard-launched
      Python restart. At the Python override layer, invalid JSON types are
      ignored and retain the true base default; Rust/dashboard deserialization
      continues to reject a present non-boolean field.
    - **C17-4 (unchanged safety):** do not activate Japanese retry replacement,
      fuzzy normalization, coherent foreign-speech translation, automatic STT
      terms, or any other shadow/off feature. Do not change scene provider,
      capture, cadence, consensus, identity, prompt, or publication lifecycle.
  - Scope:
    - `config.py`, Rust/TypeScript/Vue default contract, focused Python/Rust/Vue
      tests, and the owning `system.md` / `frontend-design.md` notes.
  - Non-goals: no frontend redesign/removal, no new runtime/API call, no other
    feature activation, no resolver or translator behavior change beyond the
    proven publication gate's initial boolean value.
  - Test gate:
    - Python source/export/override tests prove default true, explicit false,
      and invalid-type retention;
    - Rust proves missing section/field defaults true, false/true round-trip,
      non-boolean rejection, and unrelated-field tolerance;
    - Vue proves the no-config fallback is checked and an explicit false config
      remains unchecked;
    - affected full Python, Vue test/build, Rust, and diff checks pass.
  - Runtime gate: no additional paid run. T15 already exercised the active
    lifecycle; this card changes only which previously tested boolean state is
    selected when no owner override exists.
  - Codex round-1 review (2026-07-29): **REVISE**.
    - B17-1 found that T16-era JSON normally contains an explicit false, which
      is indistinguishable from an owner-selected kill switch. The proposal
      cannot both migrate that value automatically and preserve explicit false.
    - B17-2 found that invalid-type behavior must distinguish Python's
      field-level fail-closed ignore from Rust's whole-DTO parse rejection.
  - Round-2 integration response (2026-07-29):
    - B17-1 resolved conservatively: only absent section/field uses the new true
      default. Any persisted explicit false remains authoritative. No config
      migration/version state is added because the frontend is currently
      unused and the user asked for the direct pipeline default.
    - B17-2 resolved by documenting and testing the separate Python and Rust
      contracts.
  - Codex round-2 re-review (2026-07-29): **YES**. B17-1 and B17-2 are
    resolved and no new blocker was introduced.
  - Implemented 2026-07-29:
    - Direct Python pipeline runs now default
      `publish_open_set_activity=True`; the overall resolver, known-alias
      publication, manual precedence, and automatic-STT isolation are
      unchanged.
    - Rust missing-section/field defaults and the Vue no-config fallback now
      match the true pipeline default. A supplied/persisted explicit false
      remains off, including any T16-era JSON that already contains false.
    - Python ignores present invalid override types and keeps the true base;
      Rust continues rejecting a present non-boolean field.
    - No other shadow/off feature or scene/provider/prompt/cadence behavior was
      activated.
  - Validation:
    - targeted Python config/export/override suite: 62 passed / 19 subtests;
    - targeted Vue ConfigPanel: 14 passed;
    - Rust with `CARGO_INCREMENTAL=0`: 44 passed;
    - full Vue: 58 passed; production type-check/build passed;
    - full Python: 1,106 passed / 4 skipped / 245 subtests;
    - changed Rust file `rustfmt --check` and `git diff --check` passed;
    - frozen translation replay and paid runtime were intentionally omitted
      because only a previously runtime-validated activation default changed.
  - Independent post-implementation review (2026-07-29): **PASS**.
    - No findings. The reviewer verified the direct pipeline true default,
      explicit-false kill switch, Python/Rust invalid-type contracts, Rust/Vue
      missing-value defaults, conservative migration boundary, unchanged
      shadow/off features, validation evidence, and exclusion of the user's
      local `config.py` profile/comment hunk.

- [x] **T18 - Remove production-default Japanese shadow latency**
  - Evidence:
    - Live run `20260729T170655Z-394152` contained a Japanese-flagged
      translation whose primary OpenRouter attempt took about 656 ms and whose
      synchronous DeepL shadow took about 1,313 ms. Total translation latency
      was about 1,984 ms, but `reason=shadow_only` kept the original subtitle.
    - The recent scan found only 3 Japanese-flagged outputs among 5,618
      successful translations (about 0.05%). The activation gate still lacks
      the required 30 shadow events and 30 semantic labels.
  - Decision:
    - Japanese-specific quality retry now defaults `off`. Residue detection
      remains translation-quality telemetry, while explicit `shadow` and
      `active` modes remain available for intentional diagnostics/gated use.
    - An async background shadow was rejected for this card. It would require
      a bounded lifecycle-owned queue/executor, independent correlation
      events, analyzer joins, shutdown behavior, and isolation from production
      provider capacity for a very rare diagnostic.
    - `off` fails closed for every Japanese-flagged original, including a
      composite selective defect. This preserves the former default shadow
      mode's no-replacement output boundary while removing its extra API call.
      Existing selective retries on non-Japanese output remain unchanged.
  - Scope and non-goals:
    - Changed only the retry default/comment, the fail-closed Japanese `off`
      branch, focused tests, and owning architecture notes.
    - No event schema, analyzer, dashboard, provider/model route, prompt,
      correction, STT, scene, async worker, or automatic Japanese replacement
      change.
  - Proposal review:
    - Independent architecture review supported default-off over async shadow.
      It surfaced the composite-output safety boundary; the implementation
      conservatively retained the former production no-replacement behavior.
    - Final proposal re-review: **YES**, with no blocker.
  - Validation:
    - targeted config/quality-retry suite: 56 passed / 19 subtests;
    - translator core: 400 passed / 199 subtests and eval 8/8;
    - full Python: 1,108 passed / 4 skipped / 245 subtests;
    - `git diff --check` passed.
    - Frozen replay and a paid runtime were intentionally omitted: shipped
      output remains fail-closed and deterministic fake engines prove zero
      Japanese-off alternate calls. A future natural Japanese event may
      opportunistically confirm the removed latency without blocking closure.
  - Independent post-implementation review (2026-07-30): **PASS**.
    - No blocking findings. The reviewer verified default-off behavior,
      Japanese-only/composite zero-call coverage, retained quality telemetry,
      unchanged explicit shadow/active and non-Japanese selective behavior,
      documentation, and exclusion of the user's local `config.py` hunk.

- [x] **T19 - Post-T18 runtime bottleneck re-baseline — no implementation**
  - Scope: read-only use of existing schema-v3 runtime logs and
    `analyze_runtime_events.py`. No new analyzer, API call, runtime, generated
    repo report, or pipeline/config change.
  - Evidence:
    - Cross-midnight run `20260728T150713Z-222052` lasted about four hours and
      contained 1,371 translations: 1,360 success, 10 filtered, and one failed.
      Its two file segments had translation p50 750/812 ms, p95 1,422/2,062
      ms, p99 3,500/6,109 ms, and maxima 6,531/9,406 ms.
    - Run `20260729T170655Z-394152` had 130/130 successful translations with
      p50/p95/p99/max 594/1,797/2,406/2,671 ms.
    - Translation queue-wait p95 was 0 ms in every segment. Output-delay p95
      closely followed engine latency, while predecessor-stall p95 remained
      poll-scale at 63 ms. Current tail latency is provider/API time, not a
      worker-capacity bottleneck.
    - Observed OpenRouter translation cost was US$0.12090367 in the four-hour
      run and US$0.00934906 in the later short run. DeepL has no equivalent
      cost field, so these remain lower bounds on total provider spend.
    - The long run produced one attributable provider-scoped
      `total_deadline` timeout, opened the OpenRouter circuit, selected DeepL,
      then closed after two successful probes. The later run had no timeout or
      circuit event. This completes T14's natural failure/recovery evidence;
      it does not justify changing the current deadline.
    - T07 produced 33 actionable emitted outcomes in the long run and eight in
      the later run. Across two post-T07 runs, all 41 matched outcomes had zero
      strict useful merges within 300 or 500 ms. This remains below T08's
      frozen minimum of 50 and supplies no benefit signal for adding delay.
  - Decisions:
    - **T08 remains no-go:** 41/50 actionable samples and 0/41 strict useful
      outcomes cannot justify a 300-500 ms user-visible hold.
    - **T11 is obsolete as written and remains no-go:** its NVIDIA-to-
      OpenRouter hedge no longer matches the OpenRouter-primary route.
      Hedging OpenRouter with DeepL would introduce a lower-quality
      first-valid candidate, duplicate spend, non-cancellable late requests,
      and new attribution semantics for a sparse tail.
    - **More workers remain no-go:** queue wait is already zero and added
      concurrency cannot reduce provider wall time.
    - **Deadline/routing changes remain on hold:** the only observed provider
      timeout recovered correctly, while the later run was clean. Shortening
      the deadline would increase DeepL exposure; lengthening it would worsen
      the tail.
    - STT/audio/sentence quality is the next diagnostic domain, not an
      implementation card. Hard-max/forced-blob and rare STT failures are
      observable, but these logs lack heard-source ground truth and cannot
      attribute them to VAD, recognition, or assembly.
  - Cross-review:
    - Runtime review confirmed API-tail attribution, T14 open-to-close
      lifecycle, T08 counts, costs, and the absence of a worker bottleneck.
      It suggested a narrow timeout-attribution card because two failures were
      visible only through attempt chains.
    - Architecture/cost review recommended no implementation. The integration
      decision retains that result: the existing analyzer already reports
      attempt-chain hidden timeouts, T14 recovered correctly, and no
      user-visible failure or decision currently requires a new schema/card.
  - Cheapest next gate:
    - Use one future representative normal run to see whether OpenRouter
      primary attempts above 2.5/3 seconds repeat without circuit events and
      whether T07 reaches 50 actionable outcomes with any strict useful merge.
    - If upstream quality work is pursued, use a very small WAV-backed audit of
      hard-max/forced-blob cases. Do not start another broad labeling batch or
      implement an STT/audio/sentence fix without heard-source evidence.
  - Tool caveat:
    - `analyze_latency_tail.py` remains schema-v2-only and writes a fixed
      default report path, so it was not used as schema-v3 sign-off evidence.

- [x] **T20 - Per-STT-chunk Korean completeness and early sentence cut**
  - Status: **closed as no-go at the frozen shadow gate**. The pure classifier,
    record-only telemetry, analyzer support, and fixtures remain available as
    an opt-in diagnostic; active early release was not implemented, the source
    default is `off`, and config rejects `active`.
  - Owner observation (2026-07-31):
    - Live subtitles commonly accumulate roughly four to five displayed lines
      before translation appears. The current completeness behavior is not
      producing a visible latency or quality improvement.
    - After every STT chunk, the pipeline should determine whether the buffered
      Korean meaning is complete. A confidently complete sentence should be
      released immediately; an incomplete sentence should wait for the next
      chunk within existing hard bounds.
  - Verified current-path gap:
    - `SentenceBuffer.is_complete()` already classifies selected Korean
      complete/incomplete endings, and `silence_complete` can bypass the normal
      wait for a qualifying silence cut.
    - The splitter currently drains every immediately available item from
      `text_queue` into one buffer and calls `pop_ready()` only after that drain.
      It therefore does not make a cut decision after each individual chunk;
      later queued chunks can be appended before an earlier complete chunk is
      examined.
    - Outside the narrow `silence_complete` path, a complete buffer is normally
      held until `min_wait_seconds` (currently 3 seconds). This can add avoidable
      post-STT delay and allow unrelated following speech to join the sentence.
    - The current binary suffix matcher is a grammatical-tail heuristic, not a
      complete Korean semantic classifier. Ambiguous endings, unclosed
      delimiters, internal sentence boundaries, and uncertain fragments need an
      explicit fail-safe result.
  - Claims to implement:
    - **C20-1 (per-chunk decision order):** process one admitted STT chunk,
      evaluate the buffer, and handle at most one semantic early cut before
      admitting the next already-queued chunk. Re-check pause/stop before every
      admission and before emission; shutdown retains the current contract and
      does not promise to admit queued-but-unprocessed chunks. Preserve FIFO,
      pending-incomplete behavior, attribution IDs, and bounded queue behavior.
    - **C20-2 (three-way completeness):** replace the early-cut decision's
      binary interpretation with a pure deterministic result:
      `complete`, `incomplete`, or `uncertain`, plus machine-readable reasons.
      Consolidate the existing ending rules with the unfinished connector,
      particle, adnominal, and delimiter primitives currently used by
      `sentence_hold_shadow`. Unclosed delimiters and unfinished
      connector/particle/adnominal tails veto an early-complete result. Positive
      evidence is limited to safe terminal punctuation, strong Korean
      sentence-final morphology, and an explicit bounded set of short
      acknowledgements. VAD silence/pause is supporting evidence only and can
      never independently make an uncertain tail complete. Ambiguous input
      remains `uncertain`.
    - **C20-3 (early release):** a confidently `complete` buffer may bypass the
      three-second minimum wait and emit with a distinct cut reason. If a chunk
      contains a safe complete prefix followed by a meaningful residual, emit
      at most that prefix for this admitted chunk and carry the residual using
      the existing AS5 conservative approximation: every boundary-straddling
      current source becomes residual evidence and residual current/audio
      accounting is reset. This is deliberately not claimed as exact
      span-to-chunk attribution. Generic multi-prefix splitting is outside T20.
    - **C20-4 (bounded incomplete path):** `incomplete` continues waiting for a
      next chunk under the existing bounded source-count/text-length merge
      eligibility; T20 does not claim semantic merge compatibility.
      `uncertain` retains the legacy bounded timing path and cannot use the new
      semantic bypass. Active-buffer incomplete state remains distinct from a
      forced pending-incomplete cut. `force_cut_seconds`, hard audio maximums,
      pending timeout, merge limits, pause, stop, and newest-content
      backpressure remain authoritative. Tests must cover pending plus
      early-prefix residual, merge, merge-skip, timeout, pause, and stop.
    - **C20-5 (no decision API):** do not add an LLM/NLP network call for every
      chunk. The completeness decision must be local, fast, deterministic, and
      contain no I/O, lock acquisition, or runtime-event write. Benchmark it
      separately; defer telemetry writes until the admission/cut decision has
      completed, as T07 does, and gate text/sentence queue drops.
    - **C20-6 (observable and reversible):** use one narrow
      `off|shadow|active` mode, starting in `shadow`. Add backward-compatible
      schema-v3 telemetry with `(run_id, decision_id)` correlation and fixed
      fields for mode, classification/reason codes, legacy decision,
      `would_cut`, `applied`, elapsed/saved wait, drain batch position/size, cut
      outcome, current/evidence counts, residual chars/carry policy, and queue
      drops. Extend only the existing schema-v3 analyzer; schema-v2 collection
      and sampling tools cannot be sign-off evidence.
  - Proposed implementation order:
    1. Freeze a Korean completeness fixture covering reliable complete endings,
       connectors, particles, adnominal endings, quotations/brackets, short
       acknowledgements, mixed Korean/Latin text, STT punctuation absence, and
       ambiguous tails.
    2. Extract the pure three-way classifier and prove that existing complete/
       incomplete cases keep their intended result.
    3. Add the `off|shadow|active` mode configuration and validation, `shadow`
       telemetry, fixed correlation fields, schema-v3 analyzer support, and
       their focused tests without changing cut behavior. Record the queue
       drain batch and the counterfactual per-chunk decision only after
       admission/cut behavior has completed.
    4. Run one representative shadow session. If fewer than 30 would-cut
       candidates appear, drain batches are almost always one, candidates
       mostly duplicate an existing immediate safe cut, or the projected
       request growth exceeds the frozen gate, close T20 as no-go.
    5. Only after the shadow gate passes, refactor the splitter around a
       single-chunk admission helper and add the bounded active early release
       plus conservative prefix/residual handling.
    6. Add active-path concurrency/order/provenance regressions and the owning
       architecture documentation.
    7. Run targeted tests, classifier benchmark, translator-core validation,
       full pytest, frozen deterministic replay as hygiene only,
       `git diff --check`, and independent post-implementation code review; fix
       every in-scope review finding.
    8. Run one owner-controlled active session with the production default
       still off. Promote the default only if the active gate passes.
  - Frozen numerical runtime gate:
    - shadow requires at least 30 `would_cut` candidates in one representative
      session and a WAV/text-backed review of at least 20 candidates (or every
      candidate when fewer than 20 are reviewable); a single confidently
      complete false boundary fails the gate;
    - classifier benchmark over at least 10,000 representative calls must keep
      p99 below 1 ms and perform no I/O/lock/event write;
    - active applied cuts must save at least 1,000 ms at p50 from the legacy
      wait decision, with p50/p95/p99 and maxima reported;
    - translation requests per successful STT, observed translation cost per
      successful STT, and observed translation cost per runtime hour may each
      grow by at most 15% relative to the immediately preceding representative
      shadow session;
    - text-queue and sentence-queue drop counts must not increase above the
      shadow session, and no drop may be attributable to classifier/event work;
    - attribution invariant failures must remain zero. Forced/incomplete,
      filtered/too-short, and fallback-output rates may not regress by more than
      two percentage points;
    - report drain batch-size, candidate overlap with existing immediate cuts,
      sentence elapsed/chunk-count/text-length distributions, request/cost
      normalization, and every safety counter. Averages alone are insufficient;
    - failure of any safety/cost gate, lack of visible latency improvement, or
      weak/duplicate candidate coverage keeps production default off and closes
      T20 as no-go.
  - Scope:
    - sentence completeness classification, splitter admission/cut ordering,
      focused config and telemetry/analyzer support, tests, and the owning
      architecture/validation notes.
  - Non-goals:
    - no local Whisper or other local inference;
    - no STT provider/model change, dual-STT, diarization, or speaker
      separation;
    - no translation model, provider route, prompt, retry, or deadline change;
    - no subtitle layout/frontend redesign;
    - no activation of T08's generic 300-500 ms hold;
    - no broad manual annotation batch.
  - Codex architecture review round 1 (2026-07-31): **REVISE**.
    - Confirmed the drain-before-decision gap, but blocked unbounded multi-cut
      semantics, weak positive completeness evidence, silence-as-completeness,
      exact-provenance wording, unproven semantic merge compatibility,
      synchronous telemetry risk, and direct activation without a shadow gate.
    - Required lifecycle/backpressure tests, truthful AS5 residual attribution,
      fixed telemetry correlation, numerical latency/cost/safety thresholds,
      and shadow-before-active falsification.
  - Round-2 response (2026-07-31):
    - Bounded active behavior to one semantic cut per admitted chunk with
      explicit pause/stop checks and current shutdown semantics.
    - Replaced silence-based completion with narrow positive morphology/
      punctuation evidence and explicit connector/particle/adnominal/delimiter
      vetoes; ambiguity is fail-safe.
    - Retained and named the AS5 conservative residual approximation, removed
      exact attribution and semantic-compatibility claims, made classification
      pure, and deferred telemetry I/O.
    - Replaced the boolean activation with `off|shadow|active`, ordered shadow
      before implementation/active validation, and froze the numerical
      latency, cost, queue, attribution, and false-boundary gates above.
  - Codex architecture re-review round 2 (2026-07-31): **REVISE** for one new
    blocker; every round-1 blocker was confirmed resolved.
    - B20-7 found that the shadow runtime gate preceded analyzer support and
      mode validation, so its required schema-v3 report could not be produced
      before active implementation.
  - B20-7 integration response (2026-07-31):
    - Moved mode configuration/validation, fixed shadow telemetry correlation,
      schema-v3 analyzer support, and their tests into step 3. The shadow gate
      now executes and is analyzed before any active-path refactor. No third
      reviewer loop is requested; AGENTS requires an owner decision after a
      round-2 new blocker.
  - Shadow implementation and evidence (2026-07-31):
    - Added a pure local tri-state Korean completeness classifier, conservative
      first-prefix detector, per-admitted-chunk record-only
      `sentence_early_cut` telemetry, schema-v3 analyzer aggregation, config
      validation, and focused regressions. Shadow mode does not alter buffer,
      cut, merge, queue, translation, or subtitle behavior.
    - Controlled run `20260731T073755Z-450808` produced 61 correlated
      decisions and 33 counterfactual would-cut candidates. Of the 17
      candidates that did not overlap a legacy-ready cut, six were full-buffer
      candidates and 11 were prefix candidates. No text-queue or
      sentence-queue drop was observed.
    - All 61 observed drain batches had size one. This independently triggers
      step 4's frozen no-go condition: the representative run did not
      demonstrate the queue-drain ordering problem that would justify the
      active single-chunk splitter refactor.
    - The conservative actionable-candidate upper bound was 17 / 45 observed
      provider requests (37.8%). Even treating full-buffer releases as
      replacements, the run contained ten distinct actionable prefix texts
      against 45 successful translation calls (22.2% potential additional
      calls), above the frozen 15% request-growth ceiling.
    - Text review also found one confident false boundary:
      `안녕 나 방금 일어났지요 일찍 왔네 어..`. A single such boundary fails the
      frozen safety gate. The diagnostic classifier was subsequently hardened
      to reject two-or-more trailing dots and very short morphology-only
      positives such as `출게요`; first-prefix selection now bounds diagnostic
      candidate length. These fixes do not override the independent
      drain-batch and cost failures and were not used to claim a passing run.
    - After the diagnostic hardening and punctuation-tail review fix, a
      20,000-call classifier benchmark recorded p50 0.0171 ms,
      p95 0.0282 ms, p99 0.0413 ms, and max 1.0029 ms, clearing the
      p99-below-1-ms performance constraint.
  - Decision:
    - Per the pre-implementation falsification contract, stop before step 5.
      Do not add active cut mutation, do not change legacy
      `silence_complete`, and do not run an active session.
    - Retain only the opt-in shadow diagnostic so future materially different
      runtime evidence can support a new proposal without repeating the
      instrumentation work. Production remains behaviorally unchanged with
      `semantic_early_cut_mode="off"`.

- [x] **T21 - Dashboard config load fail-closed**
  - Evidence:
    - `Dashboard.vue` mounts `ConfigPanel` whenever Settings is active even
      while `get_config` is pending or after it fails.
    - `ConfigPanel.vue` turns a null config into a complete, writable frontend
      default DTO. That copy has already drifted from Python defaults (for
      example the engine chain, translation token limit, and VAD timings), and
      Rust persists the whole DTO on Save.
  - Claims to implement:
    - **C21-1:** model config loading explicitly as `loading`, `error`, or
      `loaded`. Settings may mount the editable panel only after the latest
      request succeeds; loading and persistent error states expose no Save.
    - **C21-2:** remove the complete frontend fallback DTO. `ConfigPanel`
      accepts one non-null authoritative `ConfigDto`; its editable state,
      Cancel baseline, and emitted Save payload are deep-cloned snapshots.
    - **C21-3:** every load/retry receives a monotonic generation. Only the
      latest generation may commit success or failure. A retained last-good
      value remains inaccessible while loading or failed, so stale state
      cannot overwrite the config bridge.
    - **C21-4:** keep Rust/Python config schemas, validation, restart semantics,
      process lifecycle, provider routing, and runtime behavior unchanged.
  - Non-goals:
    - no engine-chain/save-time validator expansion;
    - no process-status/readiness DTO or system-stat redesign;
    - no backend-default synchronization layer, Rust/Python change, or live
      pipeline/API run.
  - Test gate:
    - initial pending load and failed load mount no editable panel;
    - failure remains visible with Retry and cannot call `update_config`;
    - retry success mounts exact backend sentinel values;
    - stale failure after newer success, stale success after newer failure,
      and two out-of-order successes all obey latest-request-wins;
    - existing edit, Cancel, and Save behavior uses stable snapshots;
    - focused and full Vue tests, production build, `git diff --check`, and a
      fresh read-only post-implementation review pass.
  - Proposal review:
    - Round 1: **REVISE**. The reviewer required full latest-request-wins, not
      only stale-failure suppression, and required a failed reload to hide any
      retained last-good editable form.
    - Round-2 response adopted monotonic request generations and made both
      loading and error fail closed. It also froze child/parent Save snapshots
      and the latest successful load/save Cancel baseline.
    - Round-2 re-review: **YES**. Both blockers were resolved with no new
      blocker.
  - Implemented 2026-08-01:
    - Settings now has explicit loading, persistent error/Retry, and loaded
      states. Loading or failure never mounts the editable `ConfigPanel`, even
      when a last-good DTO remains in memory.
    - Config loads use a monotonic generation and only the newest request may
      commit success or failure. Regressions cover stale failure after newer
      success, stale success after newer failure, and out-of-order successes.
    - `ConfigPanel` now requires a non-null authoritative DTO. The stale full
      frontend default was removed; prop synchronization, Cancel, child Save,
      and the parent save completion use deep-cloned JSON snapshots.
    - Rust/Python config behavior, restart semantics, process lifecycle, and
      the live pipeline were unchanged.
  - Validation:
    - focused Vue baseline before implementation: 30 passed;
    - focused Vue after implementation: 33 passed;
    - full Vue suite: 61 passed;
    - production `vue-tsc --noEmit` plus Vite build passed;
    - `git diff --check` passed with line-ending notices only.
  - Independent post-implementation review: **PASS**.
    - No blocker or major finding. The reviewer verified the state lifecycle,
      latest-request-wins behavior, last-good isolation, snapshot boundaries,
      tests, and the Vue-only scope.
    - Non-blocking: no single test composes successful Save followed by
      Cancel/reload end to end; parent snapshot commit plus the existing prop
      watcher and Cancel tests cover those pieces independently. JSON cloning
      also assumes `ConfigDto` remains JSON-native, as required by IPC.

Explicit non-priorities unless new runtime evidence changes the decision:
- A text-normalization LLM on every sentence.
- Replacing the primary/fallback path with an expensive GPT/Claude model.
- More translation workers while queue wait remains negligible.
- Live SQLite cache without evidence that its historical ~0.45% hit rate has
  materially changed.
- Globally increasing history length.
- Another large manual annotation exercise.
- Reopening multilingual STT auto-detect without new evidence.
- Adding more generic examples to the full prompt.

The ordered T08-T12 Goal is complete. T08 remains deferred with 41 actionable
post-T07 outcomes and zero strict useful merge; T09 remains offline-only with
live integration rejected; T10 remains record-only; T11's NVIDIA-to-OpenRouter
hedge is obsolete under the OpenRouter-primary route and no replacement hedge
has passed its cost/quality/cancellation gate; and T12's explicit activity path
remains opt-in. T13-A/T13-B are implemented and provider-backed LoL publication
has been observed. T13-P implements provider-neutral explicit routing; its paid
fallback was explicitly configured by owner decision and its live routing gate
is complete. T14's implementation, review, normal-path, and natural
provider-failure/recovery runtime gates are complete: run
`20260728T150713Z-222052` recorded a provider `total_deadline`, circuit open,
two successful probes, and circuit close. Free-model activation remains
outside its scope.
