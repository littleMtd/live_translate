# Deterministic Pre-Architecture Fixes — Proposal (2026-06-24)

Status: Claude draft revised after Codex round-1 review (AGENTS.md workflow step 3).
Pending Codex re-review (step 4) of whether the round-1 points are resolved before
implementation. This is a proposal, not an approved plan.

Author handoff: drafted by Claude from the 2026-06-19..0624 runtime scans and the
Phase 0 replay. Scope is the four deterministic fixes that Phase 0 prioritizes over
multi-STT/resolver
(`../experiments/PHASE0_ROOT_CAUSE_REPLAY_20260624.md` Decision 6).

## How to use this document (implementer)

- Follow AGENTS.md Cross-Review Workflow. These four items are independent; review and
  implement them one at a time, not as one monolithic change. Partial delivery is valid:
  dropping or deferring any single item does not break the others (each lists its own
  artifacts). If context runs short, finish fewer items completely rather than leaving
  all four half-done.
- Each claim is tagged with an evidence type: `[code]`, `[runtime]`, `[audit]`,
  `[user-decision]`, `[assumption]`. Verify each before relying on it; unsupported
  claims are assumptions, not sign-off evidence.
- Respect the per-item Gate. Where a gate requires human audio review or human glossary
  approval, prepare the artifact and stop; do not guess past the gate.
- Existing scripts to prefer over new tooling are named per item (AGENTS.md asks to pick
  an existing script first).

## Output conventions (pinned)

- `<date>` means the run date in `YYYYMMDD` form, i.e. `20260624` for this batch. Use the
  same `<date>` for all four items so filenames line up for review.
- All generated artifacts — both the analysis `.json` and each item's proposal `.md` —
  go under `.analysis-tmp/`. Do not place proposal docs at repo root and do not `git add`
  any of them.
- Every generated `.json` embeds the exact regenerate command (script + args) in a
  top-level field so a reviewer can re-run it in one step.

## Global guardrails

- `[audit]` `config.py` may carry the user's local `streamer_profile` / profile change.
  Do not stage or commit `config.py`; do not commit/push anything unless the user asks.
- `[audit]` Do not auto-mutate `data/translation_corrections.json` or
  `data/streamer_profiles.json`. The pattern is machine-mines / human-decides
  (`scripts/suggest_corrections.py` docstring).
- `[audit]` `OPTIMIZATION_*.md` and local proposal/review docs stay untracked unless the
  user says otherwise.
- Offline analysis writes to `.analysis-tmp/`. Use the venv
  `live-subtitle-env\Scripts\python.exe`. New analysis scripts get a minimal test, run
  with `--basetemp .pytest-tmp-<name>` (default temp dir lacks permissions per
  `PHASE0_EVAL_INVENTORY_20260613.md`).
- `[audit]` Bias control (AGENTS.md): treat the symptom names below as hypotheses. Do not
  rename an observed symptom into a confirmed root cause without a cited code path.

### Schema-version coverage rule (applies to every flag-based metric)

`[runtime]` `quality_flags` (incl. `target_has_hangul`, `target_high_latin`) exist only
on `schema_version == 2` translation events. In the 24-day corpus, v1 days
(~3.1k of ~16.5k rows, the earliest dates) carry no flags. Therefore:

- Before any flag-based count, report v2 coverage as utterances and days.
- Treat v1 spans as `uncovered`, never as `clean`. A v1 day with zero flagged leaks is
  "not measured", not "no leaks".
- Any leak/quality ratio states its denominator as the v2-covered population only, and is
  marked non-extrapolable to v1 days or to production.

---

## Item 1 — Source-gated target name rendering / normalization

This overlaps the already-queued "Likely next task" in AGENTS.md
(`source-aware target correction / profile rendering hardening`) and the Task #13
runtime findings. `[audit]` Treat this item as that task, not a new idea.

Observations:
- `[runtime]` Across `runtime_events_2026*.jsonl` (24 days; v2-covered rows only),
  untranslated Hangul leaks into zh-TW output are per-profile. Examples by profile:
  `stellive_hina` 해둥이 (×73 over that profile's ~1.5k rows, last live 0611),
  `hades_chxxnnx` 채나/챈나/채냐/챈나룽 (one name, multiple STT spellings),
  `isegye_lilpa` 이파리 (×11) / 찬이, `mwmeu` 양세찬. These counts are v2-only; see the
  schema-coverage rule.
- `[runtime]` The same gap also surfaces as romanization instead of canonical rendering
  (`target_high_latin`: e.g. `-chan` instead of `Chxxnnx`, phonetic Isegye names instead
  of `Gosegu`/`Jururu`/`Lilpa`). AGENTS.md Task #13 findings already record these misses.
- `[code]` `data/streamer_profiles.json` currently contains 세구/이세돌/르르/비챤/징버거/
  아이네/릴파 but not 이파리, 찬이, 채냐. (Verify current file before acting.)

Inference (verify):
- `[assumption]` A large share of leaks is STT spelling variance of known names, so an
  exact-match glossary cannot keep up; an alias/normalization layer that maps known
  variants to one canonical source term, plus source-gated target rendering, would cover
  more than adding single spellings. Falsify by: if variant clusters are mostly unique
  one-offs rather than recurring spellings of a small name set, alias mapping has low
  yield.

Candidate change:
- Run and, if needed, extend `scripts/suggest_corrections.py` (it already detects
  `hangul_leaks` per profile and splits "STT mishearing of known names -> source_norm"
  vs "missing glossary -> profile / name_rendering_rules"). Add spelling-variant
  clustering (edit distance / jamo-phonetic) if the script does not already group them.
- Implement source-gated target correction: correct a target rendering only when the
  Korean source contains a known name (AGENTS.md candidate behavior). Do not post-edit
  target text that has no corresponding source name.

Measurement (decoupled, to avoid circularity):
- `[audit]` Codex round-1 flagged that measuring "leaks fixed" by `target_has_hangul`
  while the action is itself "replace Hangul" is self-confirming. Therefore:
  - Define the leak-detection criterion explicitly and independently of the normalization
    action (a fixed Hangul-run rule with the keep-list), and write it down in the report.
  - Report two separate numbers: (a) leak instances the candidate table *targets*, and
    (b) a re-detection pass on the rewritten output. Do not present (a) as the success
    metric; (b) only confirms the rewrite removed the targeted run, not that the rendering
    is correct.
  - Correctness of the rendering itself is not machine-decidable here; sample for human
    confirmation (see Gate).
- False positives are not a single unsourced number. Estimate FP two ways and report
  both: (1) stop-list hit count (how many candidate rewrites would touch a stop-listed
  token), and (2) a boundary-pair sample drawn for human review. Do not emit an FP rate
  without stating which of these produced it.

Gate:
- `[user-decision]` Candidate name entries require human approval before entering
  `translation_corrections.json` / `streamer_profiles.json`. Codex prepares the table;
  the user approves.
- The report must list, for human sign-off: the similarity threshold(s) used for variant
  clustering, the full stop-list actually applied, every cross-profile conflict (a
  variant that maps to different canonicals in different profiles — keep per-profile, do
  not auto-resolve), and the boundary pairs near the threshold.

Non-goals / symmetry:
- Honorifics/particles (님/씨/요) and onomatopoeia/vocalizations (예/으흥) must not be
  normalized. This list is illustrative, not exhaustive: any token of the same class
  (honorific, particle, filler, vocalization) is excluded the same way. Alternative
  considered: blanket-strip all residual Hangul in target — not proposed, because it
  would delete intentional Hangul (e.g. quoted vocalizations) and cannot tell a name from
  a particle.

Reviewer checklist (claims to validate):
- Does `suggest_corrections.py` already cluster spelling variants, or is that net-new?
- Is the leak-detection criterion written down and independent of the rewrite action?
- Is the source-gated correction applied pre- or post-translation, and does it interact
  with the existing corrections pipeline without double-application?
- Are similarity threshold, full stop-list, cross-profile conflicts, and boundary pairs
  all listed for human review?
- Is every leak number scoped to the v2-covered population?

Acceptance:
- `.analysis-tmp/name_normalization_<date>.json` (with regenerate command) + per-profile
  candidate table + targeted-vs-redetected counts + stop-list-hit and boundary-sample FP
  estimates. v2 coverage reported.
- If the correction mechanism is implemented: existing translator tests pass
  (`scripts/check_translator_core.py`) and a new test covers source-gated vs
  no-source-name negative cases. No glossary file mutated without user approval.

---

## Item 2 — Translation latency tail

Observations:
- `[runtime]` Translation latency tail is large: 0613 p99 ~11.4s, 0611 p99 ~22.9s /
  max ~120s; 0613 max 16s. Retry events are timeout-driven; 146 timeout events in the
  0613-0619 window.
- `[code]` `config.py` current values to cite as baseline: NVIDIA live path
  `live_timeout=5`, `engine_chain=("openrouter", "groq")`, `openrouter_timeout=8`,
  `groq_translation_timeout=12`. A 5s NVIDIA timeout triggers fallback; the tail may be
  fallback-chain wall time, not a single call.

Findings after attribution (`.analysis-tmp/latency_tail_20260624.json`,
`scripts/analyze_latency_tail.py`):
- `[runtime]` Over v2 success translations (recent sessions), overall p95 ~9.97s, p99
  ~14.2s, max ~120s. The tail (>=p95, n~489) is engine-latency bound: `latency_ms ~=
  engine_latency_ms` for 489/489, predecessor-stall-dominated 9, queue-dominated 8, and
  only 53/489 had an api timeout. So the tail is slow single engine calls, NOT fallback-
  chain serialization or queue/predecessor stall.
- `[runtime]` `[code]` OpenRouter is the solid lever: tail OpenRouter calls reach
  `engine_latency_ms` of ~120s (p99 ~92s) while `config.openrouter_timeout=8`. The
  configured 8s does not appear to bound these calls (~15x over). This is the clearest,
  most actionable cause of the extreme tail.
- `[assumption]` NVIDIA tail reaches ~23s vs `live_timeout=5`, but clip/offline
  translations use `timeout=60` and the events are not split by live/clip, so the NVIDIA
  "unenforced" flag is not trustworthy without resolving mode first.

Candidate change (revised by the findings):
- Verify in code whether `openrouter_timeout` is wired to the OpenRouter client's actual
  request, and if not, wire it. Expected effect: caps the ~120s outliers near 8s,
  collapsing p99/max. Metric to re-check: OpenRouter `engine_latency_ms` p99/max.
- Separately, resolve whether the NVIDIA ~23s tail events are live (should honor 5s) or
  clip (60s) before proposing any NVIDIA-side change.

(`scripts/analyze_runtime_events.py` remains the broad summary; this item adds the
focused per-engine tail attribution.)

Gate:
- `[audit]` Any `config.py` change is proposal-only here and must not be committed (local
  user changes live in that file).

Symmetry:
- Alternative explanations to test, not assume: (a) NVIDIA API-side slowness; (b)
  fallback-chain serialization after the 5s timeout; (c) predecessor stall / queue
  serialization; (d) output-delay accounting. The proposal should say which the data
  supports before recommending a knob.

Reviewer checklist:
- Is the tail dominated by fallback wall time (timeout + second engine) or by a single
  slow call? Cite the attribution.
- Are current config values cited as the baseline for the proposed delta?
- Would the proposed knob reduce p99 without raising drop/incomplete rate?

Acceptance:
- `.analysis-tmp/latency_tail_<date>.json` (with regenerate command) attribution + ranked
  causes + cited config baseline + one concrete parameter proposal with the expected
  effect and the metric to re-check.

---

## Item 3 — Short audio-overlap duplicate (surgical dedupe)

Observations:
- `[audit]` `../experiments/PHASE0_ROOT_CAUSE_REPLAY_20260624.md`: `S003`
  repeats `메이플?` across
  adjacent subtitles; the first chunk `utt-234` reports `overlap_seconds=0.4`
  `[runtime]`. `timestamp_deduped_segments=0`; the text dedupe `dedupe_transcript_overlap()`
  keeps the prefix because `메이플?` is 4 chars / below the 5-char-or-2-token threshold
  `[code]` (verify current constant in code).
- `[audit]` An existing shadow lowered the global threshold 5->4 only when the source WAV
  reports overlap and changed 24/4644 subtitles (`phase0_short_overlap_dedupe_shadow`).

Findings after the sweep (`.analysis-tmp/short_overlap_surgical_20260624.json`,
`scripts/evaluate_short_overlap_surgical.py`, all sessions):
- `[runtime]` Blast radius over all sessions (gated on `overlap_seconds>0`):
  `min_overlap_chars=4` -> 48 candidates, `=3` -> 97. (The earlier 24 was 0613-0619 only.)
- `[runtime]` The hypothesized "surgical" overlap-duration cap (remove only if the removed
  Hangul length fits `overlap_seconds * CPS`) excludes ZERO candidates: surgical==blunt at
  both thresholds. Reason `[audit]`: these candidates are by construction the removals the
  default (min 5) misses, so the newly-enabled removal is inherently <=4 chars and always
  fits even a 0.4 s window. The audio-duration cap is therefore redundant at the relevant
  thresholds; the only effective knob is `min_overlap_chars`.
- `[runtime]` `min_overlap_chars=3` surfaces risky deletions (e.g. a name `수지야`, a
  connective `그리고`), so the lower threshold raises false-deletion risk without the cap
  protecting against it.

Candidate change (revised by the findings):
- The surgical overlap-anchoring does not beat the blunt threshold here, so the change is
  simply `min_overlap_chars` 5 -> 4, gated on `overlap_seconds>0` (48 candidates). Do not
  go to 3 (names/connectives get deleted). Intentional-repetition protection cannot be
  automated at this granularity and must come from the human audio-review gate, not a cap.

Gate:
- `[user-decision]` Hard gate: the candidate list needs human audio review for
  intentional short repetitions before any live change. Passing condition stated in the
  Phase 0 doc: zero confirmed host-content deletions. Codex prepares the candidate list
  (with `audio_dump` paths) and stops.

Symmetry:
- Alternative considered: count Hangul-only length (exclude punctuation, so `메이플?`
  counts as 3) instead of overlap-anchoring. Both should be in the sensitivity sweep;
  neither ships without the audio-review gate.

Reviewer checklist:
- Is the overlap-region window defined in concrete units with a stated rationale?
- Does overlap-anchoring reduce false deletions vs the blunt rule on the same 24?
- Is the rule keyed on a real runtime field (`overlap_seconds`) available at dedupe time
  `[code]`?

Acceptance:
- `.analysis-tmp/short_overlap_surgical_<date>.json` (with regenerate command;
  window-definition sweep, surgical vs blunt side-by-side) + ready-to-listen candidate
  list. No live change before human audio review.

---

## Item 4 — Groq STT generic-error burst handling

Observations:
- `[runtime]` Two observed bursts of 5 consecutive Groq STT `reason=error` failures:
  0614 ~15:11-15:12 UTC and 0619 ~11:31 UTC. These produced no subtitle for those
  utterances. The "5" is an observation, not a chosen detection threshold.
- `[code]` `config.py`: STT `primary_engine="groq"`, `groq_max_retries=0`,
  `groq_rate_limit_cooldown_sec=60.0`, a fallback key `GROQ_API_KEY_fall_back`
  (`groq_fallback`) exists, and `sensevoice` is a selectable STT engine.

Detection definition (Codex round-1 gap):
- A burst is detected as `N` consecutive STT error events within a `T`-second window. `N`
  and `T` are parameters of this proposal, to be set and justified from the observed
  bursts, not assumed. Report the distribution of consecutive-error run lengths across
  all days so `N`/`T` are chosen from data.

Fallback-logging verification (Codex round-1 gap):
- `[assumption]` Whether a fallback engine fired during these bursts may or may not be
  recorded. First check whether the runtime schema logs a fallback / engine-switch event.
  If it does not, the report states "fallback firing cannot be determined from logs" and
  does not infer it either way.

Findings after verification (`.analysis-tmp/groq_error_burst_20260624.json`):
- `[code]` Hypothesis "generic errors are not retried" is FALSIFIED. `modules/stt.py:642-681`:
  a non-rate-limit exception triggers a one-shot cross-Groq-key retry of the same chunk
  (`_retrying_other_key`). `config.stt.groq_max_retries=0`, so the SDK itself does not
  retry; this cross-key attempt is the only retry. SenseVoice is used as init fallback and
  a ~50-call probe (`modules/stt.py:35`), not as a failover during a generic-error burst.
- `[runtime]` 189 generic-error events across 17 days; run-length distribution
  `{1:138, 2:8, 3:3, 5:2, 7:1, 9:1}`. The failed event is emitted before the retry, and
  failed events carry an empty `utterance_id`, so rescued-vs-lost is undeterminable from
  logs. Isolated singletons (138) may be rescued by the cross-key retry; multi-error
  bursts (>=3, up to 9, spanning 20-86 s, 35 error events) are consecutive failures with
  no interleaved success — consistent with both Groq keys unavailable, which cross-key
  retry cannot fix.
- `[runtime]` Error latency is bimodal (p50 1.1 ms, p90 ~10125 ms ~= `groq_timeout`), so
  `reason=error` mixes instant connection failures and client timeouts.

Candidate change (revised by the findings):
- The narrow, evidenced gap is the burst case: when both Groq keys fail for tens of
  seconds, there is no recourse and chunks drop. Detection from data: `N>=3` consecutive
  generic-error STT events (clean separation from the 138 singletons); a time guard is
  optional since the events are already consecutive (`T` ~ 90 s covers the observed spans).
- Candidate response: on a detected burst, fail over subsequent chunks to the already-loaded
  local SenseVoice until a Groq success returns, instead of continuing to drop. Reuses the
  existing SenseVoice path; does not change single-error behavior (cross-key retry already
  covers it).

Alternatives (symmetry, do not assume):
- (a) Set `groq_max_retries>0` — unlikely to help an outage hitting the same endpoint.
- (b) Lengthen cooldown — adds no recourse during the burst.
- (c) SenseVoice burst-failover — adds recourse but introduces GPU contention and a
  quality/latency difference. The proposal recommends (c) gated, with the tradeoff stated,
  but the reviewer should weigh it against (a)/(b).

Gate:
- `[audit]` `config.py`/live STT path change is proposal-then-review; do not commit
  `config.py`.

Reviewer checklist:
- What does the STT error path currently do on a generic error (cite code)?
- Does the schema log fallback events, or is fallback firing undeterminable from logs?
- Are `N`/`T` chosen from the observed run-length distribution, not assumed?
- Does the proposed response risk duplicate/late subtitles or GPU contention (if
  SenseVoice fallback)?

Acceptance:
- `.analysis-tmp/groq_error_burst_<date>.json` (with regenerate command): all bursts
  catalogued (scan every day, not only 0614/0619), consecutive-error run-length
  distribution, lost-utterance count, current-behavior code citation, fallback-logging
  finding, and one concrete policy proposal with `N`/`T`.

---

## Suggested order

1. Item 1 (highest data-backed yield; already the queued next task; biggest design).
2. Item 4 (small, bounded; verify current behavior first).
3. Item 2 (diagnose, then one knob).
4. Item 3 (shadow now; live change blocked on human audio review).

Each item ends at: analysis + shadow + proposal, or implementation only for the portion
its Gate allows. Stop at the human gates.

---

## Claude round-2 response (2026-06-24)

Codex round-1 raised 10 points. All are accepted and folded in; none required rebuttal.
Disposition and location:

Wrong-result risks:
1. Schema v1/v2 split — accepted. Added the "Schema-version coverage rule" to Global
   guardrails: report v2 coverage, mark v1 `uncovered` not `clean`, denominators are
   v2-only. Applied to Item 1 observations.
2. Item 1 circular leak/FP measurement — accepted. Added Item 1 "Measurement (decoupled)":
   leak-detection criterion written independently of the rewrite; report targeted vs
   re-detected separately; FP estimated via stop-list hits + human boundary sample, no
   unsourced rate.
3. Item 3 "overlap boundary" undefined — accepted. Added "Operational definition of
   overlap region": concrete window units + a sensitivity sweep across ≥2 definitions,
   recommend with rationale.
4. Item 4 burst params + fallback logging — accepted. Added a detection definition (`N`/`T`
   from the run-length distribution; "5" marked as observation) and a fallback-logging
   verification step (report "undeterminable" if not logged).

Output-consistency:
5. `<date>` meaning/format — pinned in "Output conventions": run date, `YYYYMMDD`,
   `20260624`, same for all four.
6. proposal `.md` location — pinned: under `.analysis-tmp/`, not repo root, not `git add`-ed.
7. Item 1 clustering threshold + cross-profile conflict — added to Item 1 Gate: list
   threshold, full stop-list, cross-profile conflicts (kept per-profile), boundary pairs;
   honorific/particle list marked illustrative-not-exhaustive.
8. Item 2 needs config baseline — added: read and cite current `config.py` values; express
   proposals as deltas.

Review-ability:
9. Regenerate command per `.json` — added to Output conventions and each item's Acceptance.
10. Independent / partial delivery — added to "How to use this document": items are
    independent, finishing fewer completely is preferred over leaving all four partial.

Open question for the user (not an implementer decision): the standard workflow is
Claude draft -> Codex review -> implement. The user has asked Claude to implement these
directly. Implementation will still honor every Gate (human approval for Item 1 entries,
human audio review for Item 3, no `config.py` commit for Items 2/4).
