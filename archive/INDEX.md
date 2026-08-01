# Archived Project Documents

Documents under `archive/` preserve historical evidence and decision context.
They are not current implementation instructions. Current work is governed by
`AGENTS.md`, its routed files under `docs/agent/`, and the Tier 1/2 documents
listed there.

Archive rather than delete when a document contains unique runtime evidence,
review findings, rejected alternatives, or decisions that may explain the
current code. Delete only exact or semantically complete duplicates after
confirming that no active document links to them.

## Classified Archive

### `roadmaps/`

- `ARCHITECTURE_PROPOSAL_CODEX_20260613.md` — superseded by
  `ARCHITECTURE_RECOMMENDATION_20260613.md`.
- `DETERMINISTIC_FIXES_PROPOSAL_20260624.md` — historical pre-implementation
  proposal; later implementation decisions and current progress are captured
  by `docs/agent/OPTIMIZATION_TODO.md` and the code/tests.

### `reviews/`

- `CODE_REVIEW_FULL_20260711.md` — completed review whose listed blocking fixes
  were closed; retained as provenance.
- `PROJECT_OPTIMIZATION_AUDIT_20260711.md` — completed multi-round audit and
  cross-review history; superseded for current execution by
  `docs/agent/OPTIMIZATION_TODO.md`.

### `experiments/`

- `GEMINI_LIVE_TRANSLATE_PROBE_20260618.md` — historical offline provider
  experiment, never a live-path plan.
- `PHASE0_ROOT_CAUSE_REPLAY_20260624.md` — completed Phase 0 replay evidence.
  It remains cited by `PHASE0_EVAL_INVENTORY_20260613.md`, but does not itself
  authorize implementation.

### `screen_ocr/`

- `SCREEN_OCR_BRAINSTORM_20260630.md` — historical ideation.
- `SCREEN_OCR_GATE_SCOUT_SPEC_20260630.md` — completed throwaway scout spec.
- `SCREEN_OCR_MVP_SPEC_20260630.md` — implemented prototype spec. Current
  operation is documented in `donation_ocr/README.md`.

## Legacy Flat Archive

Files directly under `archive/` predate this classification. Treat them with
the same historical-only rule. They may be grouped in a later mechanical pass
if doing so does not break external references.
