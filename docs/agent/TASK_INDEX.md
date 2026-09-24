# Agent Task Index

Choose task-specific references here. Search headings or task keywords first,
then read the smallest relevant section and its direct dependencies. A task
that crosses boundaries may require several rows.

## Routing Table

| Task type | Start with | Opt-in detailed references |
|---|---|---|
| Documentation-only / repository guidance | Default briefs; inspect affected instruction files | Owning domain doc only if its contract changes |
| Plan cross-review or independent direction exploration | `CROSS_REVIEW_WORKFLOW.md` and the selected plan/evidence | Owning domain contract and validation reference only for claims under review |
| Python runtime or general backend | Relevant code and tests | Search `PROJECT_CONTEXT.md` for entry points, pipeline, ownership, or state; search `system.md` for the affected architecture contract |
| Diagnosis | Relevant code, logs, tests | Search `PROJECT_CONTEXT.md` for the claimed behavior; use `VALIDATION.md` only for specialized evidence |
| Code review of implementation | `CODE_REVIEW_WORKFLOW.md`, scoped diff, relevant code and tests | Search `PROJECT_CONTEXT.md` for the claimed behavior; use `VALIDATION.md` only for specialized evidence |
| Runtime logs, failure, latency, or health claims | Runtime event schema and producer code | Search `PROJECT_CONTEXT.md` for observability/storage and `VALIDATION.md` for runtime/failure harnesses |
| Blind Phase 1 / ChatGPT Project forensics | Read `BLIND_PHASE1_WORKFLOW.md` before upload; use the exact validated run bundle | Use computer use for the Project UI; keep prior labels, causes, fixes, and other-run results out; never present offline ASR as ground truth |
| STT, capture, audio, speaker, or routing | Owning modules and tests | Search `PROJECT_CONTEXT.md` and `system.md` for audio/STT policy; search `VALIDATION.md` for STT/audio harnesses; consult Phase 0 or T25 evidence only for the specific question |
| Translation, prompt, provider, fallback, QA, or canonicalization | Owning translation modules, profile, and tests | Search `PROJECT_CONTEXT.md` for translation selection/state and `VALIDATION.md` for translator/correction harnesses; consult a current execution decision only when the task depends on it |
| SQL, cache, or persistence | Owning code and tests | Search `sql.md` for the affected schema/contract; use `system.md` only for pipeline or concurrency interaction |
| Frontend, Tauri, Rust, or Vue | Owning code and package/crate commands | Search `frontend-design.md` for the affected component/IPC section; use `system.md` only for a runtime bridge |
| Existing script or maintenance command | Search script name in repository | Search `TOOL_INVENTORY.md` for ownership/capability and `VALIDATION.md` for its validation domain |
| New or substantially changed tool, harness, replay, analyzer, benchmark, or sampler | `VALIDATION_BRIEF.md` and repository search | Search `TOOL_INVENTORY.md` and the relevant `VALIDATION.md` section before proposing or editing |
| Optimization direction, TODO selection, evidence gate, or card progress | Default briefs and relevant code/evidence | Search `OPTIMIZATION_TODO.md` by card ID or keyword; search other detailed docs only for the selected card's dependencies |
| Architecture review or roadmap work | Default briefs and affected code | Search the relevant current decision in `ARCHITECTURE_RECOMMENDATION_20260613.md`; use `PHASE0_EVAL_INVENTORY_20260613.md` only for Phase 0 policy; add domain docs by boundary |
| Archived or superseded evidence | `archive/INDEX.md` | Open only the indexed artifact and its named current replacement |
| Whole-project review | Establish explicit review dimensions first | Route each dimension separately; do not automatically ingest every roadmap, backlog, or historical file |

## Reference Status

### Detailed current references (opt-in)

- `docs/agent/CROSS_REVIEW_WORKFLOW.md`: plan review, independent exploration,
  and post-implementation reviewer procedure.
- `docs/agent/CODE_REVIEW_WORKFLOW.md`: implementation code review procedure.
- `docs/agent/PROJECT_CONTEXT.md`: detailed repository/runtime map.
- `docs/agent/VALIDATION.md`: specialized validation and evaluation routing.
- `docs/agent/TOOL_INVENTORY.md`: utility inventory and ownership.
- `docs/agent/BLIND_PHASE1_WORKFLOW.md`: current natural-production evidence,
  computer-use, and blind-review handoff contract.
- `docs/agent/OPTIMIZATION_TODO.md`: large optimization backlog and card history.
- `system.md`: backend/runtime architecture contract.
- `sql.md`: database and cache contract.
- `frontend-design.md`: desktop/frontend architecture and IPC reference.
- `ARCHITECTURE_RECOMMENDATION_20260613.md`: adopted roadmap plus later decision addenda; search for the relevant decision rather than treating every historical phase as current work.
- `PHASE0_EVAL_INVENTORY_20260613.md`: Phase 0 evaluation/speaker policy where applicable.

### Candidate and historical references (never implementation authority alone)

- `ARCHITECTURE_PROPOSALS_20260612.md`
- `ARCHITECTURE_PROPOSAL_QUALITY_CEILING_20260614.md`
- `CODEX_REVIEW_PROMPT_QUALITY_CEILING_20260614.md`
- `archive/` and task-specific evidence reports, including T25 artifacts

Candidate work requires an explicit user decision promoting it into current
scope. Archived material must be checked through `archive/INDEX.md` first.

Do not broaden into unrelated history merely because it shares a file.
