# Runtime Bundle Evidence Guide

## Evidence hierarchy

1. `runtime_events.jsonl` or ordered `runtime_events.partNNN.jsonl` files are the
   primary persisted evidence.
2. `manifest.json` is a machine-readable index with counts, observed time span,
   profile state sequence, providers, sanitization notes, file order, source
   locators, and integrity metadata.
3. `CHATGPT_PROJECT_README.md` and `subtitles.tsv` are derived navigation aids.
4. `audio_index.json` describes run-linked utterance audio. `audio/` is optional.

Derived views can be incomplete or mistaken. Resolve disputes against raw
events, while remembering that exported events are sanitized copies of what was
persisted.

## Universal identity and time

Runtime schema rows normally carry `schema_version`, `event_type`, `run_id`,
`created_at`, `run_kind`, `git_sha`, and `git_dirty`. Use `run_id` in every join.
Use `(run_id, sequence_id)` for translation sequencing. Manifest source
provenance maps derived ordinals back to the original daily filename and line.

Raw bundle parts preserve source-file and line order. When constructing a
chronological view, sort parsed timestamps with raw part/line order as the
stable tie-break. Do not reorder equal timestamps and then infer causality.

## Event families

- `audio_startup`, `audio`: capture readiness and chunk/VAD evidence.
- `stt`: provider request/result, filtering, confidence and utterance evidence.
- `sentence`: assembly/cut and frozen profile/activity evidence.
- `provisional_translation`: one-shot candidate lifecycle where enabled.
- `translation`: source, final target, provider attempts, deterministic
  disposition, history/cache attribution, and publication fields.
- `translation_fallback`: circuit, fallback, or probe state when emitted.
- `profile_resolution` and scene/activity events: privacy-safe observations,
  consensus, rejection, activation, and generation evidence.
- subtitle lifecycle events: display/revision actions where emitted.

Unknown event types are not noise. Retain them in the timeline and describe
only their persisted fields unless current documentation defines them.

## Important joins

- `utterance_id`, `source_utterance_ids`, `evidence_source_utterance_ids`:
  audio/STT to sentence/translation provenance.
- `sentence_id`: assembled source to translation request.
- `sequence_id` or translation/request ID: final ordering and result.
- `provisional_id`, subtitle ID, revision: provisional-to-final lifecycle.
- `profile_generation`, profile/cache identity: immutable context ownership.
- history cohort fields: which prior-state partition was eligible.
- attempt index, route ID, selected status: provider chain ownership.

Identifiers may be absent on older events. Never synthesize a join solely
because timestamps are nearby.

## Evidence limitations

- STT text is observed downstream source, not audio ground truth.
- Missing audio cannot be reconstructed.
- Filtered STT text may not be persisted if it never reached a later sentence.
- Missing or pre-persistence events cannot be inferred as facts.
- Runtime emission is best effort; a bundle contains all persisted matching
  records, not proof that no event was lost before persistence.
- Screenshots, raw scene frames, complete window titles, and raw vision responses
  are intentionally absent.
- The included config may be a latest unbound sanitized dashboard snapshot, not
  the historical effective config for the selected run.
- Observed first/last event and duration are not necessarily true process
  start/end/lifetime.
- A provider response can precede rejection; only final publication evidence
  identifies user-visible output.
- General final display timestamps are unavailable when no corresponding
  lifecycle event was persisted.
- Sanitized secrets and local paths cannot be recovered from the bundle.
