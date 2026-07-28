# CLAUDE.md

This file is the entry point for Claude Code.

## Read Order

1. `system.md` — project architecture and runtime rules
2. `sql.md` — database schema and persistence behavior
3. `frontend-design.md` — desktop UI (Tauri + Rust + Vue.js) architecture

## Document Roles

| Document | Scope | Status |
|----------|-------|--------|
| `system.md` | Runtime pipeline, architecture, deployment roadmap | Reflects current code |
| `sql.md` | SQLite schema, cache behavior, thread safety | Phase 1 ✅ implemented |
| `frontend-design.md` | Tauri app structure, Rust handlers, Vue components | Phase 2 🚧 in progress (handlers + components scaffolded under `src-tauri/`, `src-frontend/`) |

## Rules

- **Do not duplicate** architecture between documents. Each document owns its domain.
- **Read all three** if a task spans backend + frontend + database.
- **Treat each as source of truth** within its domain:
  - `system.md` → app structure, pipeline design
  - `sql.md` → database behavior, cache logic
  - `frontend-design.md` → UI architecture, IPC design

## Task Routing

**Backend (Python, STT, Translator, DB):**
- Read: `system.md` + `sql.md`

**Frontend (Tauri, Rust, Vue.js):**
- Read: `frontend-design.md` + `system.md` (for context)

**Full Stack (Backend + Frontend integration):**
- Read: all three documents

## Before Implementation: Which Roadmap Docs Apply

This repo accumulates proposal/roadmap markdown files. Before starting any
implementation ("施工"), use this tier system to decide which ones are binding
vs. reference-only. (Same rule is duplicated in `AGENTS.md` for Codex; if the
two ever disagree, treat that as a bug and reconcile both.)

**Tier 1 — Mandatory, by task scope:** `system.md` / `sql.md` /
`frontend-design.md` per the Task Routing table above.

**Tier 2 — Current execution plan (defines "what to build now"):**
- `ARCHITECTURE_RECOMMENDATION_20260613.md` — adopted execution plan.
- `PHASE0_EVAL_INVENTORY_20260613.md` — Phase 0 decisions/policy; refines or
  overrides the corresponding parts of the recommendation (e.g. speaker
  policy). If the two conflict, `PHASE0_EVAL_INVENTORY` (the newer decision)
  wins.

**Tier 3 — Process rules (defines "how"):**
- `AGENTS.md` — cross-review workflow. Any implementation task must follow it
  (Claude drafts → Codex reviews → ... → implement only once no blockers
  remain).

**Tier 4 — Candidate/future directions, NOT implementation-ready:**
- `ARCHITECTURE_PROPOSALS_20260612.md` — idea pool. Its own stated rule: new
  ideas go here, do not start work from it directly.
- `ARCHITECTURE_PROPOSAL_QUALITY_CEILING_20260614.md` +
  `CODEX_REVIEW_PROMPT_QUALITY_CEILING_20260614.md` — quality-ceiling
  candidate list. v3 explicitly does not change Phase 0/1 priority; even the
  items marked "can run in parallel" are offline/read-only validation steps,
  not implementation.

**Core rule:** implementation requires Tier 1 + Tier 2 read first; Tier 3
governs process; Tier 4 items only become implementation-ready after the user
explicitly decides to promote one, at which point Tier 2 must be updated (or a
new execution-plan doc created) — never implement directly from a Tier 4 doc.

Historical reviews, completed proposals, and superseded experiments live under
`archive/`; they are evidence, not current instructions. Read
`archive/INDEX.md` before relying on an archived document, and prefer the
replacement named there.
