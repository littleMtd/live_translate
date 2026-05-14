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
