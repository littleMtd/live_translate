# Agent Brief

This is the compact project context for routine work. Read it after the root
`AGENTS.md`, then use `TASK_INDEX.md` to select task-specific material.

> Do not read large reference documents in full by default. Search for the relevant section first.

## Authority and Evidence

- The user's current request and explicit decisions define the authorized task.
- `AGENTS.md` owns repository-wide process, review, delegation, completion,
  commit, and safety rules.
- A current, task-selected execution decision or TODO card defines what to build.
- Domain documents define intended contracts within their scope, but examples
  and inventories can become stale. Verify claims about current behavior against
  code and runtime evidence.
- Candidate proposals and archived material are evidence, not implementation
  authority. Read `archive/INDEX.md` before relying on archived material.
- Treat task names and suspected bug names as hypotheses. Keep observations,
  inferences, and proposed fixes separate until evidence connects them.

## Repository Map

- `main.py`: CLI/runtime entry point and mode orchestration.
- `config.py`: checked-in defaults and supported configuration surface.
- `modules/`: capture, STT, sentence assembly, translation, policy, correction,
  output, observability, and persistence modules.
- `profiles/`: profile-specific vocabulary and translation behavior.
- `src-tauri/`: desktop shell and Rust commands.
- `src-frontend/`: Vue frontend.
- `tests/`: Python test suite.
- `scripts/`: established replay, benchmark, audit, and maintenance utilities.
- `logs/`, `scratch/`, and generated reports: runtime/evaluation artifacts; do
  not assume they are safe to mutate or representative without checking origin.
- `.env`: secrets and local credentials. Never print or commit secret values.

## Current Runtime Shape

The ordinary live path is conceptually:

`Windows capture -> STT -> sentence assembly/provisional request -> translation workers -> ordered output`

Current defaults and fallbacks must be verified in `config.py` and the owning
modules before changing or reporting them. At the time this brief was created,
the live configuration supported ElevenLabs Scribe v2 with a Groq same-chunk
fallback, and DeepSeek V4 Flash with protected translation fallbacks. These
provider names are a snapshot, not a substitute for inspecting code.

The current translation correctness owners are deliberately separate:
canonical obligations, source-grounded unknown-name escrow, semantic
terminology escrow, deterministic corrections, script/meta publication guards,
and final fail-closed invariants. Provider primary/fallback successes and exact
provisional promotion converge on the same finalization/publication path.
Quality retry and the former translation shadow experiments are retired; this
does not retire current source normalization or Hangul/Kana safety guards.

Preserve these cross-cutting invariants unless the task explicitly changes them:

- A failed fallback or shadow path must not corrupt the user-visible primary path.
- Ordered output must remain ordered even when processing is concurrent.
- Compare providers using the same source, profile, activity, and context.
- Diagnostic metadata and user-visible subtitle quality are different signals.
- Runtime events prove that an event occurred; they do not alone prove root cause.
- Configuration overrides must stay within their documented ownership and
  persistence boundary; do not silently invent a second source of truth.

## Working Safely

- The worktree may already be dirty. Preserve unrelated user changes and inspect
  scoped diffs before editing.
- Do not stage, commit, push, delete, or broadly rewrite files unless authorized.
- Do not call paid APIs unless the user explicitly authorizes the paid call.
- Prefer existing scripts and harnesses before creating a new one.
- Keep changes inside the requested task. A useful adjacent improvement is not
  automatically authorized.
- For application changes, read `VALIDATION_BRIEF.md` before editing and validate
  proportionally to risk.

## Documentation Ownership

- `AGENT_BRIEF.md`: compact facts needed by most tasks.
- `VALIDATION_BRIEF.md`: common local validation workflow.
- `TASK_INDEX.md`: task-to-document routing and opt-in references.
- `PROJECT_CONTEXT.md`: detailed runtime map and ownership reference.
- `VALIDATION.md`: specialized harness, replay, labeling, and evaluation details.
- `TOOL_INVENTORY.md`: established utility inventory.
- `OPTIMIZATION_TODO.md`: optimization backlog, evidence gates, and card history.
- Root architecture/domain documents: detailed contracts and historical decisions.

Update the owning document when a task changes its contract. Do not copy a
detailed policy into multiple entry points.

## Default Start Checklist

1. Read `AGENTS.md` and this brief.
2. Open `TASK_INDEX.md` and select only the row(s) matching the task.
3. Search headings or keywords in routed references before opening a section.
4. Read `VALIDATION_BRIEF.md` before code/config/data behavior changes or when
   choosing validation.
5. Inspect the relevant code and scoped worktree status.
6. Follow the cross-review and completion workflow in `AGENTS.md` when it applies.
