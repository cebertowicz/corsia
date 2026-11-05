---
name: task-discipline
description: Use when accepting, performing, re-scoping, delegating, waiting on, or closing work with more than one step, whenever a thread holds multiple tasks, and before substantive research, analysis, design, recommendations, artifacts, or mutations—procedures for aligning on the request, groundwork, verification, the plan, and work with no established workflow.
---

# Task discipline

This skill is how to align on a request, ground your work in what exists, and verify it before calling it done. It applies to every agent, subagent, and task size.

## Alignment

Before substantive work, record your reading on the task: `intent` holds the outcome, for whom, what done means, what is out, and how you settled what the request left open; `intent_words` quotes the request verbatim. Mark an inferred part of `intent` `ASSUMED:` with its source.

## Groundwork by work type

Inspect the target where used, then its owners and prior art:

- **Research:** authoritative sources, counterevidence, decision and audience.
- **Design or product:** composed current surface, prior decisions, reusable parts.
- **Code or operations:** source, tests, configuration, runtime, prior fixes.
- **Documents or data:** source material, canonical artifact, target format, rendered or calculated result.

Search the user's words and the underlying concept. Retrieve the applicable domain requirements, decisions and failure cases from the owner's canonical reference; citing its index alone is insufficient. Name what you reuse or amend, and why existing candidates fail before creating another.

Check each factual premise you need to proceed before building on it. Probe the intended combination and composed experience, not just each capability separately; record the actual result and revise the approach when it fails. A limit or target needs a source, measurement or explicitly confirmed assumption.

## Verification methods

Check the result against the aligned acceptance evidence on the real artifact or surface: recalculate, render, run, re-read against sources, or test with a reader. Bind fresh evidence to each acceptance item. Use an independent checker when a wrong result is costly, and give it the intent and the primary artifact. For behavior changes, record the success signal, failure signal, and when it will be measured.

### Persistent-owner readiness

Before claiming domain expertise is ready, compare representative outcomes with a fresh agent carrying only general System knowledge. Require cheaper, faster and/or better results; include knowledge upkeep/retrieval, planning, execution, review, rework and user attention. Test the savings hypothesis that comprehensive expert plans let weaker workers preserve quality at lower total cost. Run the comparison with skill `experiment-design` under its controls for published or general claims, and count task difficulty among the factors held fixed. Instruction compliance alone proves no outcome benefit. Sample behavior and outcomes first; consider mechanical formats or enforcement only if instructions fail.

## No established workflow

Look first for a workflow or skill that fits the work, and use it. If none does and you will do this kind of work again, beyond a step or two, make one with skill `workflow-design`. A one-off is a plan: do it directly.

## Recurring reviews

Use schedules, not re-armed watches. Record cadence, mechanism and last run under recurring workflows; close a converted watch's node. Respect the person's pause: no runs or catch-up. Run a cheap script check before a model turn; batch questions. One owner per shared risk. Add a plan task only when a run finds work. Cancel reviews no longer needed and close their nodes; when reviews merge, keep the rarer check's timing.

## Plan

Read the plan fresh before changing it. Each node has a plain title and acceptance criteria; use each plan operation for its meaning. Argument, revision, and retry rules are in the tool schemas. A mutation counts only when a fresh read shows the intended durable state; if tools are absent or state disagrees, report a plan-capability failure rather than substituting a Markdown plan.

- **New input.** A correction, addition, or narrowing of the same outcome edits its node before you continue. A separate deliverable gets its own node; to switch to it, settle, close, or park the current task (the tool names what holds your slot). A stop halts the named work exactly. Order by consequence and deadline, never drop accepted work, and delegate separable work whatever its urgency.
- **Size.** How long a capable person would need to deliver this result by hand, including the reading, thinking, drafting, checking and revising it takes; not how long you will take. XS under 15 min, S under 1 h, M under 4 h, L under 16 h, XL under 80 h, XXL more; unknown if you cannot tell. Set `size` on every task you add or insert.
- **Waits.** When work waits on an agent, system, or external event, arm a watch with a deadline and start your next ready task. Blocked is only for what the user must do. A watch can miss, so recheck waits whenever you next act.
- **Dependencies** express real prerequisites between nodes. Add the edge instead of writing only “wait for X.” An accepted prerequisite can make work eligible; it does not override other holds or an occupied executor slot. Follow the tool's next-work result and supported selection notifications. The prerequisite may sit in another agent's plan in your project: find it with `plan_read(thread_id=...)`, then `add_dependency` on its node; you are woken when it is done, fails, or is dropped. For work outside any readable plan, wait on its work object with an event/deadline watch.
- **Settle.** Finished work is accepted or failed; replaced or unneeded work is closed with a reason. Fail only when the objective cannot be met. A tooling defect keeps the node open: route the defect and wait on its fix with a dependency or watch.
- **Objective changes.** Edit the node first; ask the requester only when the change is material.

## Declared workflows

If applying (not just reading or reviewing) a skill whose `corsia.yaml` (named by SKILL.md's `metadata.corsia-data`) has `workflow.steps` and whose **Applying these steps** covers this run, add one `phase` with `workflow_ref` (skill name), `difficulty` and `size`. CORSIA builds its steps: `check` becomes acceptance criteria, `after` dependencies, instruction the description, and a nested `workflow:` a phase. Do not add those steps yourself. A step's `difficulty` overrides the phase's. Rate the work: is the outcome clear, the method known, and the work one piece? Three yes is routine, one no standard, more hard. Skip a reached optional step with `close_node` and a reason; finish a required step with evidence or `fail` it. Add your own step as a task under the phase with `workflow_ref`, `workflow_step` `+<short-name>`, `difficulty` and a reason. Correct a wrong rating through `difficulty_rerated`.

## Delegated work

The parent owns the node, briefs the child (skill `agent-team-decomposition`), attaches it, verifies the return against the node's acceptance criteria, and settles the node. The child keeps its own plan and returns a result with evidence. If a return cannot be harvested, keep the node open, route the defect, and re-brief or wait. Reuse or stand-down of a finished worker is not promised; use what the delegation tools return.

If these steps are repeatedly skipped despite correct delivery, record the missing mechanical gate as a capability gap instead of adding prose.
