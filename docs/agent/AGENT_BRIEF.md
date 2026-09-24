# Agent Brief

Compact project orientation only. `AGENTS.md` owns process and safety rules;
`TASK_INDEX.md` routes task-specific references. Verify current behavior in
code and runtime evidence before changing or reporting it.

## Repository Map

- `main.py`: CLI/runtime entry point and mode orchestration.
- `config.py`: checked-in defaults and supported configuration surface.
- `modules/`: capture, STT, sentence assembly, translation, policy, correction,
  output, observability, and persistence modules.
- `data/`: profile, terminology, correction, and evaluation data; owning code
  is under `modules/`.
- `src-tauri/`: desktop shell and Rust commands.
- `src-frontend/`: Vue frontend.
- `tests/`: Python test suite.
- `scripts/`: established replay, benchmark, audit, and maintenance utilities.
- `logs/`, `scratch/`, and generated reports: runtime/evaluation artifacts; do
  not assume they are safe to mutate or representative without checking origin.
- `.env`: local credentials; follow the secret-handling rule in `AGENTS.md`.

## Runtime Orientation

The ordinary live path is conceptually:

`Windows capture -> STT -> sentence assembly/provisional request -> translation workers -> ordered output`

Verify provider defaults and fallback behavior in `config.py` and the owning
modules. Use `TASK_INDEX.md` to locate detailed translation, profile, and
runtime contracts in `PROJECT_CONTEXT.md` and the domain documents.

Across runtime tasks, preserve ordered output and keep failed fallback paths
from corrupting the primary result. Diagnostic metadata and subtitle quality
are different signals; an event record alone does not establish root cause.
Compare providers using the same source, profile, activity, and context;
configuration overrides must follow their documented ownership boundary.

Prefer existing scripts and harnesses before creating new ones. The worktree
may be dirty; inspect the scoped diff before attributing changes to your task.
