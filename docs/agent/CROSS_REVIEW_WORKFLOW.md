# Cross-Review Workflow

The plan cross-review below applies when the user requests it, when a selected
task plan or TODO card requires it, or when the task changes application,
configuration, or data behavior across multiple owners and needs a scoped plan.
For bounded fixes and documentation-only changes without a selected plan or
explicit cross-review request, use scoped inspection and validation without
requiring a Claude Code proposal. The current user request authorizes the work
it describes; the review process does not add a separate approval requirement.

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
- Parallel ideation does not bypass task routing or proposal-promotion rules, the
  cross-review workflow, evidence gates, or the one-card-at-a-time
  implementation boundary. Multiple agents may explore different cards, but
  they must not concurrently modify overlapping implementation scope.
- Skip parallel exploration when the task is straightforward and bounded, the
  perspectives would substantially duplicate one another, relevant evidence is
  unavailable, or the user asks for a single-agent pass.

Workflow for tasks requiring plan cross-review:
1. Claude Code drafts the task plan section.
2. Codex cross-reviews the proposal and writes the Codex review subsection.
3. If Codex finds blockers, Claude Code revises only the relevant
   plan/scope/test sections and adds a Claude round-2 response subsection.
4. If step 3 occurs, Codex re-reviews only whether the previous Codex blockers
   are resolved. If round 2 introduces a new blocker, label it explicitly as a
   new blocker.
5. If blockers remain after step 4, stop and ask the user to decide. Do not
   loop indefinitely between reviewers.
6. Codex implements after there are no blockers or after the user explicitly
   decides to proceed.

If Claude Code is unavailable, the primary agent may draft the proposal and
request an independent read-only proposal review before implementation. Record
who actually drafted and reviewed it; never label substitute work as Claude
Code output.

The selected cross-review workflow authorizes updates to that task's plan and
review subsections. Do not modify unrelated review documents unless the user
explicitly asks.

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

## Post-Implementation Review

For application, configuration, or data behavior changes, after implementation
and validation Codex immediately starts a separate read-only reviewer agent
with fresh context, whether or not the task needed plan cross-review. The
reviewer checks scoped `git status`, `git diff`, tests, replay/blast-radius
output, and any plan checklist while the primary agent prepares the completion
report. Do not require the user to relay the task to Claude Code.

Give the reviewer only the scoped task summary, changed-file list, and test
evidence; do not fork the full conversation history unless the review genuinely
depends on it. The reviewer should not rerun the full suite when the primary
agent already supplied a complete passing result. The reviewer must not edit
files, stage, commit, push, or expand implementation scope.

If it returns `REVISE`, the primary agent independently verifies the findings,
fixes those supported by evidence, reruns affected validation after each fix,
and asks the reviewer to check the revised diff and results. Continue while
evidence-backed findings within scope remain; do not treat a fixed number
of review or validation attempts as completion. Return unresolved design
disagreements or validation that cannot be completed to the user rather than
exchanging repetitive reviews without new evidence. If a reviewer agent is
unavailable, perform the scoped self-check and state that limitation in the
completion report.
