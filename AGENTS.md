# Agent Instructions

## Before Implementation: Which Roadmap Docs Apply

This repo accumulates proposal/roadmap markdown files. Before starting any
implementation ("施工"), use this tier system to decide which ones are binding
vs. reference-only. (Same rule is duplicated in `CLAUDE.md` for Claude; if the
two ever disagree, treat that as a bug and reconcile both.)

**Tier 1 — Mandatory, by task scope (see `CLAUDE.md` Task Routing):**
`system.md` / `sql.md` / `frontend-design.md`.

**Tier 2 — Current execution plan (defines "what to build now"):**
- `ARCHITECTURE_RECOMMENDATION_20260613.md` — adopted execution plan.
- `PHASE0_EVAL_INVENTORY_20260613.md` — Phase 0 decisions/policy; refines or
  overrides the corresponding parts of the recommendation (e.g. speaker
  policy). If the two conflict, `PHASE0_EVAL_INVENTORY` (the newer decision)
  wins.

**Tier 3 — Process rules (defines "how"):** this file — the cross-review
workflow below applies to any implementation task.

**Tier 4 — Candidate/future directions, NOT implementation-ready:**
- `ARCHITECTURE_PROPOSALS_20260612.md` — idea pool. Its own stated rule: new
  ideas go here, do not start work from it directly.
- `ARCHITECTURE_PROPOSAL_QUALITY_CEILING_20260614.md` +
  `CODEX_REVIEW_PROMPT_QUALITY_CEILING_20260614.md` — quality-ceiling
  candidate list. v3 explicitly does not change Phase 0/1 priority; even items
  marked "can run in parallel" are offline/read-only validation steps, not
  implementation.

**Core rule:** implementation requires Tier 1 + Tier 2 read first; Tier 3
governs process; Tier 4 items only become implementation-ready after the user
explicitly decides to promote one, at which point Tier 2 must be updated (or a
new execution-plan doc created) — never implement directly from a Tier 4 doc.

Historical reviews, completed proposals, and superseded experiments live under
`archive/`; they are evidence, not current instructions. Read
[`archive/INDEX.md`](archive/INDEX.md) before relying on an archived document,
and prefer the replacement named there.

## Cross-Review Workflow

Bias-control rules:
- Treat bug names, task filenames, and user shorthand as hypotheses, not proof.
- Separate observations, inferences, and proposed fixes. Do not rename an
  observed symptom into a confirmed root cause until code/runtime evidence
  supports it.
- When drafting prompts or proposals, include plausible alternative
  explanations and ask reviewers to verify or falsify them.
- Runtime evidence can prove that an event occurred; it does not by itself prove
  which code path caused it or that it explains unrelated error classes.
- Prefer neutral terms such as "suspected", "observed", "candidate cause", and
  "over-count" until the responsible code path is cited.
- For labeling or quality investigations, distinguish user-visible subtitle
  quality from diagnostic metadata quality. A sample can have acceptable output
  while still having contaminated attribution evidence.

Parallel direction exploration:
- When the task is to discover optimization opportunities, choose a development
  direction, compare architectures, or investigate several plausible causes,
  the primary agent should use multiple agents concurrently when the question
  can be divided into genuinely independent perspectives.
- Normally assign two or three read-only agents distinct briefs, for example:
  runtime/data evidence and current bottlenecks; architecture/safety and race
  risks; cost/latency/operational trade-offs; or alternative designs and
  falsification tests. Adapt the briefs to the task instead of always using the
  same categories.
- Give each agent a bounded question, relevant files/evidence, non-goals, and
  the required output format. Prefer fresh or minimal context so agents reach
  independent conclusions rather than merely echoing the primary agent's
  framing.
- During this exploration the agents may inspect code, tests, logs, replay
  artifacts, and existing documents, but must not edit files, call paid APIs,
  start implementation, stage, commit, push, or broaden scope unless the user
  separately authorizes that action.
- The primary agent should investigate the integration view in parallel, then
  deduplicate the proposals, identify agreements and contradictions, verify
  important claims against code/runtime evidence, and present one synthesized
  recommendation or ordered TODO list. Agent vote count is not evidence.
- Each retained proposal should state its evidence, expected benefit, principal
  cost/risk, dependency, and the cheapest test or runtime signal that could
  confirm or reject it. Unsupported ideas remain hypotheses.
- Parallel ideation does not bypass the Tier 1/Tier 2 promotion rule, the
  cross-review workflow, evidence gates, or the one-card-at-a-time
  implementation boundary. Multiple agents may explore different cards, but
  they must not concurrently modify overlapping implementation scope.
- Skip parallel exploration when the task is straightforward and bounded, the
  perspectives would substantially duplicate one another, relevant evidence is
  unavailable, or the user asks for a single-agent pass.

Default task workflow:
1. Claude Code drafts the task plan section.
2. Codex cross-reviews the proposal and writes the Codex review subsection.
3. Claude Code revises only the relevant plan/scope/test sections and adds a
   Claude round-2 response subsection.
4. Codex re-reviews only whether the previous Codex blockers are resolved. If
   round 2 introduces a new blocker, label it explicitly as a new blocker.
5. If blockers remain after step 4, stop and ask the user to decide. Do not loop
   indefinitely between reviewers.
6. Codex implements after there are no blockers or after the user explicitly
   decides to proceed.
7. After implementation and validation, Codex immediately starts a separate
   read-only reviewer agent with fresh context. The reviewer checks the scoped
   `git status`, `git diff`, tests, replay/blast-radius output, and plan
   checklist while the primary agent prepares the completion report. Do not
   require the user to relay the task to Claude Code.
   Give the reviewer only the scoped task summary, changed-file list, and test
   evidence; do not fork the full conversation history unless the review
   genuinely depends on it. The reviewer should not rerun the full suite when
   the primary agent already supplied a complete passing result.
8. The reviewer agent must not edit files, stage, commit, push, or expand the
   implementation scope. If it returns `REVISE`, the primary agent fixes the
   evidence-backed findings and reruns the affected validation once; unresolved
   design disagreement goes back to the user rather than looping agents.

When asking Claude Code to draft a plan, use a proposal format, not a persuasive plan.

Claude Code proposal rules:
- Do not write conclusion-framing phrases like "no open decisions",
  "reviewer should directly agree", "obvious", or unsupported "low risk".
- Do not pre-fill reviewer answers.
- Do not frame a previous reviewer conclusion as something Claude Code must accept.
- Ask Claude Code to verify reviewer claims first; if it agrees, revise the plan,
  and if it disagrees, rebut with code/data/runtime evidence.
- Use neutral language like "verify this claim" instead of directive language like
  "must fix", "must include in scope", or "this is a blocker", unless the user has
  already made that decision explicitly.
- Every claim must have an evidence type: code, runtime, audit, user decision, or assumption.
- Alternatives must be symmetrical; do not strawman unchosen options.
- Assumptions must be explicit and include how to verify or falsify them.
- Reviewer checklist must list claims to validate, not confirmation questions.

When asking Codex to review, use verification format:
- Verify each Claim ID against code/data/runtime/audit/user-decision evidence.
- Unsupported claims can be assumptions, but cannot be sign-off evidence.
- Check goal fit, non-goals, assumptions, test adequacy, and post-implementation validation.
- Give YES / REVISE / NO only after claim verification.
- Do not implement or push during cross-review.

## Implementation Completion Reports

After implementation work, start the step-7 read-only reviewer agent directly
and include its verdict in the completion report. Do not generate a prompt for
the user to relay to another tool.

Implementation completion reports must include:
1. Modified file list.
2. `git status --short` summary.
3. `git diff --stat` summary.
4. Exact test commands that were run.
5. Test output summary.
6. Plan checklist results, item by item.
7. Scope deviation status.
8. Blockers, non-blocking risks, and post-implementation validation items.
9. Independent reviewer-agent verdict, or the concrete reason it could not run.

Do not push, do not stage, and do not modify review documents unless the user
explicitly asks.

## Routed Project References (Mandatory)

`AGENTS.md` is the short entry point and owns global precedence, review,
delegation, completion, commit, and safety rules. Detailed material is split by
responsibility so agents do not load the entire project history for every task.

Before acting, read each applicable routed file completely:

- [`docs/agent/PROJECT_CONTEXT.md`](docs/agent/PROJECT_CONTEXT.md) — required
  before code changes, architecture review, diagnosis, or claims about current
  runtime behavior. Contains the verified repository/runtime map and ownership
  boundaries.
- [`docs/agent/VALIDATION.md`](docs/agent/VALIDATION.md) — required before code
  changes and whenever selecting tests, replay/runtime evidence, labeling, or
  evaluation methods. Contains existing-tool-first rules and validation routing.
- [`docs/agent/TOOL_INVENTORY.md`](docs/agent/TOOL_INVENTORY.md) — required
  before proposing, adding, or substantially changing a script, harness,
  analyzer, replay, benchmark, sampler, or maintenance command.
- [`docs/agent/OPTIMIZATION_TODO.md`](docs/agent/OPTIMIZATION_TODO.md) —
  required for optimization planning, choosing/advancing a TODO card, checking
  evidence gates, or updating implementation/runtime progress.

Routing rules:
- Read only the applicable routed files, but read a selected file completely.
- These files are binding extensions of `AGENTS.md`; `AGENTS.md` wins if they
  conflict. More specific Tier 1 domain documents still own their stated
  technical domain, and newer Tier 2 decisions still override older roadmap
  proposals as defined above.
- A user request to inspect the whole project or rethink optimization direction
  requires all four routed files.
- When a task changes architecture, validation workflow, tool ownership, or
  TODO status, update the owning routed file instead of growing `AGENTS.md`.
- Do not duplicate detailed content back into `AGENTS.md`; keep this entry file
  focused on routing and global process.
