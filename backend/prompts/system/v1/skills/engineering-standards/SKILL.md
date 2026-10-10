---
name: engineering-standards
description: Use when diagnosing, debugging, writing, reviewing, or refactoring code or runtime behavior — trace conceptual forks and root causes, establish intent before calling behavior a defect, and prevent default engineering failures.
corsia:
  handler:
    kind: instruction
---

# Engineering standards

Approach each change from first principles and optimize for the whole system, not the local task: the full user journey, robustness, and architectural fit. Prefer the simplest correct root-cause fix. Do not hide defects behind duplicate abstractions, one-off paths or silent fallbacks. A deliberate mitigation needs an explicit limitation, recovery/removal condition and the authority its actual trade-off requires. For product-affecting work, complete the product and design stages the project's delivery process defines before coding.

## Code ground

Apply skill `task-discipline` groundwork to the code and record a compact result in the work object:

- the canonical existing owner or reusable path for every effect, and whether you correct, call, replace, or cannot reuse it;
- affected callers, sibling paths, contracts, configuration, tests, and user surfaces;
- persisted and runtime state that must survive, including migration obligations;
- failure paths: which component is responsible for each and what the user or operator sees;
- for stateful work, one canonical state owner and the allowed success, failure, retry, and recovery transitions.

Search exact names and the underlying concept before adding anything. Unresolved ownership, consumers, or state survival blocks implementation. Evaluate related paths, shared components, docs, tests, and contracts for impact, but preserve the authorized outcome and explicit exclusions. Necessary supporting changes are in scope unless a boundary or material unresolved decision prevents them. Check loading, empty, error, stale, unauthorized, and recovery states where relevant. If a change alters what a user sees, clicks, or receives, verify the composed result through the surface they use. For risky or core logic, write the test first.

## Approach gate

Record the cause, how the approach removes or controls it, and why the existing owner cannot already do the job. Prefer deleting the competing path, making invalid states unrepresentable or correcting the canonical owner. New complexity must explain what becomes simpler or more reliable. Retries and reconciliation can be legitimate for transient/distributed failures; prove their lifecycle, idempotency and bounds rather than adding them to conceal an unexplained failure. A mitigation is reported as such, with remaining cause and removal condition. Honor explicit rejected approaches.

## Debugging

1. Capture exact input, environment and build (model/harness where relevant). Seek a deterministic reproduction; for intermittent failures, record timing and occurrence evidence and state what remains unisolated.
2. Capture the evidence before changing anything.
3. Hold one hypothesis and name the observation that would falsify it. Bisect by evidence, not guessed flags.
4. Read the code path the working case uses before writing a parallel one; the fix usually belongs there.
5. Instrument rather than speculate.
6. Done: a test that failed for the intended reason now passes, the symptom is gone on the real surface, siblings of the same class were checked, and the root cause is recorded.

## Conceptual forks

When apparently equivalent cases behave differently, or an unchanged retry gives a different result, suspect competing concepts, state owners, or paths until disproven. Trace both branches, name the canonical concept and owner, then consolidate or justify keeping both. Do not patch the second branch to imitate the first; tests must prove the fork is gone or intentional.

### Establish intent before calling behavior a defect

Observed behavior proves what happens, not that it was chosen. Before filing a defect or proposing to change code you did not write, inspect the implementing comments, tests asserting the behavior, and the introducing change or requirement. Record the intent evidence. If it is documented as deliberate, it is a design disagreement: address it to the owner, name the tradeoff you would change, and identify what authority could supersede the old choice. If intent is unknown, say so.

Treat every finding (reviewer, test, investigation) as evidence, not an instruction to implement its remedy. Group related findings, find the shared cause, evaluate the blast radius of the problem and the proposed fix, and choose the smallest coherent solution. Do not bolt machinery on per finding.

## Default failures to prevent

Fixing symptoms not causes; coding only the happy path; assuming APIs or invariants unchecked; creating a new component or abstraction before proving the current one inadequate; duplicating shared logic; patching one surface while forgetting adjacent flows; ignoring state transitions and recovery; hiding problems behind silent fallback; declaring success unverified; sending a diff to review unreviewed by you (the reviewer confirms clean work; it is not your bug net).

## Code health

Keep code easy to reason about: single responsibility, explicit flat control flow, clear names, low complexity, reused patterns. Follow the project's stated file, function, and complexity limits; if it states none, do not invent numbers, but split before a file or function is too large to review whole. Multiple registries, flags, callbacks, or repair paths claiming one lifecycle signal a redesign.
