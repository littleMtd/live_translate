# Agent Instructions

## Compact Onboarding and Documentation Routing

Default reading path:

1. This file for global process and safety rules.
2. [`docs/agent/AGENT_BRIEF.md`](docs/agent/AGENT_BRIEF.md) for compact project facts.
3. [`docs/agent/TASK_INDEX.md`](docs/agent/TASK_INDEX.md) to select task-specific references.
4. [`docs/agent/VALIDATION_BRIEF.md`](docs/agent/VALIDATION_BRIEF.md) before
   application/config/data behavior changes or when choosing validation.

Do not read large reference documents in full by default. Search for the relevant section first.

Domain contracts, roadmap documents, detailed runtime references, backlogs,
and evidence reports are opt-in through `TASK_INDEX.md`. A current user-approved
task or selected execution decision defines what to build; candidate proposals
and archived material do not authorize implementation. Read `archive/INDEX.md`
before relying on archived evidence.

Precedence is: current user decision and scope; this file's global process and
safety rules; task-selected current decisions; scoped domain contracts. Verify
claims about current behavior against code/runtime evidence. If documents
conflict, prefer the more specific and newer authorized decision, and update the
owning document rather than duplicating policy across entry points.

## Global Safety Rules

- Inspect the scoped worktree before editing and preserve unrelated user changes.
- Do not stage, commit, push, delete, or broadly rewrite files unless the user
  explicitly authorizes that action.
- Do not call paid APIs without explicit user authorization.
- Never print or commit secrets or local credentials.
- Keep changes within the authorized task. An adjacent improvement is not
  automatically in scope.
- Modify review documents only for the selected cross-review task or when the
  user explicitly requests it.

## Blind Phase 1 Isolation

Before a Blind Phase 1 review, read
[Blind Phase 1 Workflow](docs/agent/BLIND_PHASE1_WORKFLOW.md). Use only the
validated bundle for the exact `run_id` from the user's fresh natural run; if
that evidence is missing or fails integrity checks, stop. Do not substitute
external media or another run. Use computer use for the ChatGPT Project UI and
provide no known failure labels, suspected causes, proposed fixes, results from
other runs, or offline ASR as ground truth. Treat returned findings as leads
requiring independent verification before any production change.

## Task Workflows

### Plan cross-review

Use plan cross-review when the user requests it, a selected task plan or TODO
card requires it, or an application/configuration/data behavior change spans
multiple owners and needs a scoped plan. Bounded fixes and documentation-only
changes without a selected plan or explicit cross-review request need only
scoped inspection and validation. The user's current request authorizes the
work it describes; review adds no separate approval requirement.

When applicable, read [Cross-Review Workflow](docs/agent/CROSS_REVIEW_WORKFLOW.md)
before drafting or reviewing a proposal. For optimization direction,
architecture comparisons, or several plausible causes, use independent
read-only perspectives when they can genuinely divide the question; the
linked workflow defines the limits and synthesis requirements.

### Code review

For a review of implementation changes or behavior, read
[Code Review Workflow](docs/agent/CODE_REVIEW_WORKFLOW.md). Code review is
read-only by default. Review the changed implementation surfaces and the
cross-module paths needed to establish their behavior; a backend-only request
excludes frontend code. Keep plan review and code review verdicts separate.

### Codex agent settings

When an agent runs the Codex CLI (`codex exec`) for this repository, pass these
as command-line flags instead of editing `~/.codex/config.toml`:

- Use the newest installed `codex.exe` (compare `codex --version` across the
  Codex app's `%LOCALAPPDATA%\OpenAI\Codex\bin\*\` copies and the VS Code
  extension's `bin\windows-x86_64\`). Older CLIs do not know newer models and
  the API rejects them ("not supported when using Codex with a ChatGPT
  account"); on 2026-10-08 `gpt-6.1-sol` worked with 0.162 but not 0.155.
- Plan cross-review, re-review, and post-implementation review:
  `-m gpt-6.1-sol -c model_reasoning_effort="high" -s read-only`.
  Reviews are the backstop for implementation mistakes; low-effort reviews
  missed a truncated production prompt and a contradictory profile instruction
  (2026-10-08, `docs/agent/ISEGYE_GROUP_PROFILE_PLAN_20261008.md` §8).
- Implementation: `-m gpt-6.1-sol -s workspace-write`, keeping the user's
  configured default reasoning effort.
- The `workspace-write` sandbox cannot write `.git`, so the invoking agent
  commits only after its own validation and the user's authorization. Point
  pytest temporary and cache paths at the system temp directory
  (`--basetemp`, `-o cache_dir`); sandbox-created directories in the project
  root can end up locked to the sandbox account.

## Implementation Completion Reports

For application, configuration, or data behavior changes, start a separate
read-only reviewer agent directly after validation, following the
post-implementation steps in
[Cross-Review Workflow](docs/agent/CROSS_REVIEW_WORKFLOW.md#post-implementation-review), and include its
verdict in the completion report. Do not generate a prompt for the user to relay
to another tool. For documentation-only and other bounded non-behavior changes,
use a scoped self-check; an independent reviewer is optional.

If the reviewer returns `REVISE`, verify and fix evidence-backed findings,
rerun affected validation, and request re-review. Continue while actionable
in-scope findings remain; escalate unresolved design disagreements to the user.

Completion reports for application, configuration, or data behavior changes
must include:
1. Modified file list.
2. `git status --short` summary.
3. `git diff --stat` summary.
4. Exact test commands that were run.
5. Test output summary.
6. Plan checklist results, item by item, or `not applicable` if no formal plan
   exists.
7. Scope deviation status.
8. Blockers, non-blocking risks, and post-implementation validation items.
9. Independent reviewer-agent verdict, or the concrete reason it could not run.

For documentation-only changes, report the changed files, scoped diff, and
validation performed. Global safety and review-document rules above still
apply.
