# Validation Brief

This is the common local validation path. Specialized evaluation, replay,
labeling, STT, and analyzer guidance remains in `VALIDATION.md` and is routed by
`TASK_INDEX.md`.

> Do not read large reference documents in full by default. Search for the relevant section first.

## Core Rules

- Validate the smallest behavior boundary that can falsify the change, then
  widen only when risk or evidence requires it.
- Use an existing test, replay, analyzer, or benchmark before adding a new tool.
- Keep offline evaluation separate from live production behavior.
- Do not call paid APIs without explicit user authorization.
- Do not treat a successful API response, clean output format, or empty QA flag
  set as proof of translation/subtitle quality.
- Record exact commands and concise results for the completion report.
- Preserve unrelated dirty files and inspect only the scoped diff.

## Common Workflow

1. Identify the changed behavior and its owning module or document.
2. Search `tests/` and `scripts/` for an existing narrow validation path.
3. Run the narrowest deterministic tests first.
4. If concurrency, ordering, fallback, persistence, or runtime telemetry changed,
   run the relevant integration/replay check.
5. Run the broader suite appropriate to the touched surface.
6. Inspect `git status --short`, scoped `git diff`, and `git diff --stat`.
7. Confirm no generated artifacts, secrets, caches, or unrelated changes entered
   the scoped diff.
8. Complete the independent read-only review required by `AGENTS.md`.

## Test Selection

Search for repository-specific commands before assuming these examples are
current. Typical local choices are:

- Python narrow test: `python -m pytest <test-file-or-node> -q`
- Python full suite: `python -m pytest -q`
- Frontend checks: use the scripts declared in `src-frontend/package.json`.
- Rust/Tauri checks: use the crate commands appropriate to `src-tauri/`.
- Documentation-only changes: verify links/paths, headings, line counts, scoped
  diffs, and instruction consistency; application test suites are normally not
  necessary unless documentation generation is executable.

## When to Open Detailed Validation Guidance

Search within `VALIDATION.md` for the matching section when the task involves:

- deterministic translation corrections or canonicalization;
- translator maintenance or provider evaluation;
- runtime/failure evidence, replay, or blast-radius claims;
- STT, audio capture, speaker handling, or frozen audio assets;
- prompt/API experiments;
- labeling, attribution, or quality-evaluation methodology;
- test collection hygiene or temporary artifact handling.

Search `TOOL_INVENTORY.md` by script name or capability before proposing a new
script, harness, analyzer, replay, benchmark, sampler, or maintenance command.

## Completion Evidence

At minimum, retain:

- changed-file list;
- `git status --short` summary;
- `git diff --stat` summary;
- exact validation commands;
- pass/fail/output summary;
- checklist status and scope deviations;
- blockers and remaining risks;
- independent reviewer verdict.

The authoritative completion-report format remains in `AGENTS.md`.
