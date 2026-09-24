# Code Review Workflow

Apply this workflow automatically when the user asks for a "code review",
"review", "review the code", "審查程式碼", or an equivalent review of
implementation changes. Use it for reviews of code or implementation behavior,
not for reviews of plans, proposals, or reviewer claims governed by the
[Cross-Review Workflow](CROSS_REVIEW_WORKFLOW.md). If a request contains both
kinds of review, apply each workflow only to its own artifact and keep their
verdict formats separate.
Code review is read-only by default: do not modify application code, tests, or
project artifacts, and do not stage, commit, or push. Review the implementation
surfaces changed by the task and the cross-module paths needed to establish
their behavior. A backend-only request excludes frontend code; a change that
crosses the frontend, Tauri, and backend boundaries includes the relevant
parts of each.

### Phase 1 — Discovery

Begin with the current change and its relevant implementation control flow. Search
broadly enough to find plausible defects rather than limiting inspection to the
surface diff, but do not perform an indiscriminate whole-repository scan on
every review. Trace relevant functions, callers, callees, state owners, related
modules, and existing tests only as needed to establish behavior. Pay
particular attention to:

- cross-module calls and control flow;
- asynchronous execution, races, and thread or worker lifecycles;
- state management, transitions, and data consistency;
- provider routing, fallback, retry, and rate limiting;
- startup, shutdown, cleanup, and error propagation;
- cache identity, invalidation, and context consistency;
- stale results, duplicate suppression, and provisional/final lifecycles;
- contract violations, boundary conditions, and untested execution paths; and
- diagnostics, telemetry, and forensic correctness.

Record every candidate finding as an unverified claim. Each claim must cite a
concrete code location and describe its trigger, erroneous execution path, and
practical impact. Do not present a suspected cause as established fact.

### Phase 2 — Independent Verification

Independently verify every discovery claim; never trust it merely because it
came from another model, agent, or earlier review. For each claim:

1. Trace the actual code and complete relevant control flow.
2. Confirm whether the current architecture can reach the trigger.
3. Look for guards, rollback, isolation, or compensation that prevents or
   limits the issue.
4. Compare the behavior with applicable documentation, contracts, and tests.
5. Create a minimal reproduction or run focused tests when needed and safe.
6. Verify that the claimed user-visible or operational impact follows.

Classify each claim as:

- **Confirmed**: both the defect and claimed impact are supported.
- **Partially Confirmed**: the core defect exists, but some claimed triggers or
  impacts do not.
- **Unconfirmed**: available evidence is insufficient for a determination.
- **False Positive**: current control flow or safeguards exclude the issue.

Passing tests are not proof that no defect exists. Never report an unexecuted
test as passing, and never promote an unverified hypothesis to a fact.

### Phase 3 — Final Report

Report only the integrated discovery and verification results, not raw
unverified findings. The report must include:

- a final **APPROVE** or **REVISE** verdict;
- confirmed issues with evidence-based severity;
- exact files and line numbers;
- trigger conditions, erroneous path, and actual impact;
- a repair direction and missing regression tests;
- disposition of partially confirmed, unconfirmed, and false-positive claims;
  and
- exact tests executed and their results.

Order findings by practical consequence:

1. blockers that break core behavior or an important system contract;
2. confirmed functional defects;
3. diagnostics, telemetry, or forensic-correctness defects; and
4. schema consistency, code quality, or improvement suggestions.

Do not label an improvement suggestion as a bug, or inflate a diagnostic-data
problem into a core-function failure. Determine severity from verified impact,
trigger conditions, and existing safeguards rather than the discovery claim's
initial rating.

### Model and Agent Assignment

The primary agent may delegate bounded investigation to available subagents
when independent perspectives could materially improve the review. Choose
models and reasoning effort according to the available capabilities, task
complexity, and expected value; the repository cannot require a particular
model or number of subagents.
Do not end Discovery merely because an initial plausible defect has been found.
When deeper architectural reasoning, complex control-flow analysis, or
unresolved interactions could materially change the findings, the primary
agent may delegate further investigation to an available higher-reasoning
subagent.
The primary agent retains responsibility for orchestration, evidence
integration, independent verification of every discovery finding, and the
final report. Keep the verification context independent enough to challenge
discovery claims. If subagents are unavailable, the current agent must execute
the phases in order and disclose that limitation; never claim that a model or
subagent was used when it was not actually invoked.
