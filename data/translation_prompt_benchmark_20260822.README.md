# Production-Derived Translation Prompt Benchmark

This benchmark freezes 75 real production translation inputs for offline
comparison of the current compact capsule with future prompt candidates. It is
an evaluation artifact only: it does not implement Candidate A or Candidate B,
change production prompts, or define a new production policy.

Files:

- `translation_prompt_benchmark_20260822.json` — cases, provenance, production
  state, behavioral expectations, and scoring metadata.
- `translation_prompt_benchmark.schema.json` — structural contract.

## Case selection

Cases were selected from the 2026-08-15, 2026-08-16, 2026-08-19,
2026-08-20, 2026-08-23, and 2026-08-26 runtime event files and their corresponding
translation logs. Every case resolves to an exact runtime `(run_id,
sequence_id)`. A translation-log reference is null when production suppressed
the subtitle before publication; otherwise it resolves to an exact source
line. Selection pairs observed failures with nearby or same-class successful
controls rather than treating the production output or current QA flags as
ground truth.

Coverage includes canonical/profile behavior, fluent semantic and domain
errors, unknown names and script boundaries, ordinary Korean, corrupted STT,
incomplete or segmented input, history/activity disambiguation, numbers and
units, multilingual speech, tone, and meaningful repetition. There are no
synthetic cases in this version.

`case_role` determines intended use:

- `expected_improvement`: prompt-owned behavior a candidate should improve.
- `control`: ordinary or matched successful behavior that must remain sound.
- `regression_control`: STT-, segmentation-, repetition-, or other upstream
  behavior. Prompt changes are not expected to repair the source.
- `policy_comparison`: the invariant is fixed, but more than one exact output
  policy can be legitimate (notably unresolved names).

`owning_layer` is an evidence-based attribution, not a demand that the prompt
fix every case. Cases marked `stt` or `sentence_segmentation` are deliberately
regression controls.

## Effective profile facts and truncation

The production compact capsule calls
`modules.translation_prompts.get_translation_profile_facts()`. That function
sends only the text before the first blank line in the profile's Qwen prompt.
The JSON records, for every active Qwen profile, the exact effective prefix,
its SHA-256 digest, line counts, and the omitted suffix.

All six active profiles currently have an omitted suffix. The practical split
is:

| Profile | Reaches compact capsule | Omitted after first blank line |
|---|---|---|
| `irise` | fixed member, fandom, company, and title mappings | duo/fandom background sentence and examples |
| `stellive_hina` | initial proper-name and game glossary | examples, personality/domain notes, common-role guidance |
| `isegye_lilpa` | Gosegu, Jururu, Lilpa, Official髭男dism mappings | examples and detailed streamer/profile notes |
| `hades_chxxnnx` | member/fan mappings plus Minecraft and server-owner rules | examples, detailed group notes, and the current-stream hot glossary |
| `mwmeu` | member/fandom mappings, hot glossary, and personality paragraph | examples |
| `url` | group/member/company/title mappings | two examples |

This benchmark preserves that current behavior. It does not promote omitted
facts into the effective prompt.

## Reconstructing production messages

An evaluator must use the current production builders named in
`production_message_contract` and construct each request as follows:

1. Build the compact system capsule using the stored `profile_id`,
   `effective_profile_facts`, and stored `activity.capsule`.
2. Append each stored history pair in order, using the production
   `[CONTEXT ONLY — DO NOT TRANSLATE OR REPEAT]` user label and its observed
   production target as the assistant message.
3. Append `source_text` under the production
   `[CURRENT INPUT — TRANSLATE ONLY THIS]` label.
4. Preserve the stored incomplete/forced state when evaluating a prompt design
   that consumes those signals. The current compact message builder does not
   add separate incomplete/forced text to the model messages.

The history was reconstructed from the ordered completed runtime prefix using
the production profile/activity/cohort eligibility constraints. The artifact
freezes these stored messages for future runs, so comparisons must not retrieve
newer translation memory.

Provider transport envelopes are out of scope. OpenRouter and direct DeepSeek
share bounded history and current-input structure, but each uses its own
production system prompt and provider-specific effective-message builder.

## Scoring

Apply hard gates first. Any applicable hard-gate failure fails that case,
regardless of prose quality. Gates cover canonical tokens and forbidden
variants, publication script, numbers/units, sentence type and roles, and
unsupported entity/event/fact invention.

Recent live regressions may also contain `semantic_expectations`. These are
narrow literal checks (`required_any_substring`, `required_all_substrings`, or
`forbidden_substring`) plus explicitly marked `manual` constraints for roles,
unknown identities, or lifecycle behavior. Literal checks are deterministic;
manual constraints remain human review. They are deliberately not a generic
LLM semantic judge.

The 2026-08-23 cases additionally freeze `production_trace`: the first provider
attempt, every recorded fallback attempt, and the final publication outcome.
Run `scripts/evaluate_translation_prompt_benchmark.py
--production-runtime-baseline` to score those recorded outputs offline. This
mode makes no provider calls and distinguishes semantic expectation results
from the existing script/canonical/number gates.

To score a new complete result set without embedding provider execution in the
evaluator, pass `--results <json>`. The file must contain exactly one result for
each of the 75 case IDs. The maintained evaluator intentionally has no retired
prompt/model experiment modes and no API-calling option.

Then score each listed soft dimension from 0 to 2:

- semantic fidelity;
- grammatical roles and direction;
- domain sense;
- completeness without invention;
- natural zh-TW;
- tone/slang;
- conservative handling of noisy input.

For unknown names, do not score omission as intrinsically correct. Enforce the
behavioral invariant: no fabricated Chinese phonetic name, fabricated
romanization, or unsupported identity change; preserve supported sentence
meaning as far as possible. Exact unresolved-name handling remains available
for prompt-policy comparison.

Domain judgments must respect `domain_evidence_scope`: source-supported sense,
activity/history-supported disambiguation, and unsupported semantic invention
are separate outcomes. Activity may support a hypothesis without proving that
a corrupted source actually contained that word.

## Repeated runs and comparison

Run cases with `repeat_count: 3` three times. Cases with `repeat_count: 1`
receive one initial pass because they are lower-variance or upstream lifecycle
controls;
upstream and segmentation controls are classified independently by
`owning_layer` and `case_role`, not by repetition count. Increase any case to
five repetitions only when repeated results disagree or the aggregate sits
near the predeclared decision boundary.

Report both:

- per-case hard-gate unanimous pass and any-failure rate; and
- mean soft scores by dimension, class, role, profile, and owning layer.

A candidate is acceptable only if it improves its targeted prompt-owned
classes without hard-gate regression or meaningful soft-score regression on
matched controls. Do not merge STT/segmentation controls into the claimed
prompt-improvement score, and do not use current QA flags as labels.

Cases marked `required_pending` need later audio verification. Until
verified, they may measure conservative robustness to the stored noisy source,
but they cannot establish what was actually spoken.

Optional `source_evidence_status` distinguishes source-text-only evidence,
strong multi-ASR support with human listening pending, context-supported
hypotheses, audio-confirmed truth, and unresolved evidence. Agreement between
ASRs is supporting evidence, not ground truth.

Case 073 no longer treats Chinese profanity as a model invention: the retained
ElevenLabs source explicitly contains `fucking`, and faster-whisper
independently returned `f***ing`. Its historical invented-profanity assumption
was disproven, while the exact audio remains human-verification pending.

## Maintenance rule

Do not silently refresh profile facts, activity capsules, history, or expected
behaviors. A production behavior change requires a versioned benchmark update
with provenance revalidation so baseline and candidate continue receiving the
same frozen input.
