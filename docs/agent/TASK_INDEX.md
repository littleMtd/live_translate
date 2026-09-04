# Agent Task Index

Use this file to choose task-specific documentation after reading `AGENTS.md`
and `AGENT_BRIEF.md`. Read `VALIDATION_BRIEF.md` before changing application,
configuration, or data behavior, or before selecting validation.

> Do not read large reference documents in full by default. Search for the relevant section first.

Use heading search (`rg "^#" <file>`) or a task keyword first, then read the
smallest relevant section plus any directly referenced dependency. A task that
crosses boundaries may require several rows.

## Routing Table

| Task type | Start with | Opt-in detailed references |
|---|---|---|
| Documentation-only / repository guidance | Default briefs; inspect affected instruction files | Owning domain doc only if its contract changes |
| Python runtime or general backend | Relevant code and tests | Search `PROJECT_CONTEXT.md` for entry points, pipeline, ownership, or state; search `system.md` for the affected architecture contract |
| Diagnosis or code review | Relevant code, logs, tests | Search `PROJECT_CONTEXT.md` for the claimed behavior; use `VALIDATION.md` only for specialized evidence |
| Runtime logs, failure, latency, or health claims | Runtime event schema and producer code | Search `PROJECT_CONTEXT.md` for observability/storage and `VALIDATION.md` for runtime/failure harnesses |
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

- `docs/agent/PROJECT_CONTEXT.md`: detailed repository/runtime map.
- `docs/agent/VALIDATION.md`: specialized validation and evaluation routing.
- `docs/agent/TOOL_INVENTORY.md`: utility inventory and ownership.
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

## Search Examples

- List sections: `rg -n "^#{1,6} " docs/agent/PROJECT_CONTEXT.md`
- Find a TODO card: `rg -n "T[0-9]+|<keyword>" docs/agent/OPTIMIZATION_TODO.md`
- Find a tool: `rg -n "<script-or-capability>" docs/agent/TOOL_INVENTORY.md scripts tests`
- Find ownership: `rg -n "<module-or-feature>" docs/agent/PROJECT_CONTEXT.md system.md`

If a selected section depends on definitions elsewhere, follow those direct
links. Do not broaden into unrelated history merely because it shares a file.
