---
name: task-discipline
description: Use when accepting, performing, re-scoping, delegating, waiting on, or closing work with more than one step, whenever a thread holds multiple tasks, and before substantive research, analysis, design, recommendations, artifacts, or mutations—procedures for the alignment, groundwork, and verification gates, the plan, and work with no established workflow.
---

# Task discipline

Core context defines the alignment, groundwork, and verification gates and their exits. This skill is how to meet them, and it applies to every agent, subagent, and task size.

## Alignment

Core context's Alignment gate is the procedure. Mark an inferred part of `intent` `ASSUMED:` with its source.

## Groundwork by work type

Inspect the primary target where it is actually used, then the owners and prior art that could already answer the need:

- **Research:** authoritative sources, counterevidence, decision and audience.
- **Design or product:** composed current surface, prior decisions, reusable parts.
- **Code or operations:** source, tests, configuration, runtime, prior fixes.
- **Documents or data:** source material, canonical artifact, target format, rendered or calculated result.

Search the user's words and the underlying concept. Retrieve the applicable domain requirements, decisions and failure cases from the owner's canonical reference; citing its index alone is insufficient. Name what you reuse or amend, and why existing candidates fail before creating another.

Apply the core groundwork gate to factual premises needed to proceed. Probe the intended combination and composed experience, not just each capability separately; record the actual result and revise the approach when it fails. A limit or target needs a source, measurement or explicitly confirmed assumption.

## Verification methods

Check the result against the aligned acceptance evidence on the real artifact or surface: recalculate, render, run, re-read against sources, or test with a reader. Bind fresh evidence to each acceptance item. Use an independent checker when a wrong result is costly, and give it the intent and the primary artifact. For behavior changes, record the success signal, failure signal, and when it will be measured.

### Persistent-owner readiness

Before claiming domain expertise is ready, compare representative outcomes with a fresh agent carrying only general System knowledge. Require cheaper, faster and/or better results; include knowledge upkeep/retrieval, planning, execution, review, rework and user attention. Test the savings hypothesis that comprehensive expert plans let weaker workers preserve quality at lower total cost. Run the comparison with skill `experiment-design` under its controls for published or general claims, and count task difficulty among the factors held fixed. Instruction compliance alone proves no outcome benefit. Sample behavior and outcomes first; consider mechanical formats or enforcement only if instructions fail.

## No established workflow

Look first for a workflow or skill that fits the work, and use it. If none does and you will do this kind of work again, beyond a step or two, make one with skill `workflow-design`. A one-off is a plan: do it directly.

## Plan

Read the plan fresh before changing it. Each node has a plain title and acceptance criteria; use each plan operation for its meaning. Argument, revision, and retry rules are in the tool schemas. A mutation counts only when a fresh read shows the intended durable state; if tools are absent or state disagrees, report a plan-capability failure rather than substituting a Markdown plan.

- **New input.** A correction, addition, or narrowing of the same outcome edits its node before you continue. A separate deliverable gets its own node; to switch to it, settle, close, or park the current task (the tool names what holds your slot). A stop halts the named work exactly. Order by consequence and deadline, never drop accepted work, and delegate separable work whatever its urgency.
- **Size.** How long a capable person would need to deliver this result by hand, including the reading, thinking, drafting, checking and revising it takes; not how long you will take. XS under 15 min, S under 1 h, M under 4 h, L under 16 h, XL under 80 h, XXL more; unknown if you cannot tell. Set `size` on every task you add or insert.
- **Waits.** When work waits on an agent, system, or external event, arm a watch with a deadline and start your next ready task. Blocked is only for what the user must do. A watch can miss, so recheck waits whenever you next act.
- **Dependencies** express real prerequisites between nodes. Add the edge instead of writing only “wait for X.” An accepted prerequisite can make work eligible; it does not override other holds or an occupied executor slot. Follow the tool's next-work result and supported selection notifications. The prerequisite may sit in another agent's plan in your project: find it with `plan_read(thread_id=...)`, then `add_dependency` on its node; you are woken when it is done, fails, or is dropped. For work outside any readable plan, wait on its work object with an event/deadline watch.
- **Settle.** Finished work is accepted or failed; replaced or unneeded work is closed with a reason. Fail only when the objective cannot be met. A tooling defect keeps the node open: route the defect and wait on its fix with a dependency or watch.
- **Objective changes.** Edit the node first; ask the requester only when the change is material.

## Declared workflows

If you are applying a skill (not just reading or reviewing it) whose `corsia.yaml` (named by `metadata.corsia-data` in its SKILL.md) has `workflow.steps`, and its **Applying these steps** note covers this run, create one phase for this run, then under it one task per step you will do. Each task's acceptance criteria is the step's `check`; its dependencies are the step's `after`. To skip an optional step, create no task: add `skipped: <step id>, <reason>` to the phase description, and give its dependents the skipped step's `after` instead (repeat if that step is skipped too). A required step whose check is already met still gets a task, completed normally. Before starting, rate each task's difficulty: the step's `difficulty` wins; if it has none, ask yourself: is the wanted result clear, is there a known way to do it here, is it one piece? Three yes is routine, one no standard, more hard. Also set each task's `size` (see **Size**). On add or insert, pass `workflow_ref` (the skill name), `workflow_step` (the step id, or `+<short-name>` for an added step), and `difficulty`. A step that names `workflow:` gets a phase instead of a task, set up exactly as a task would be (acceptance criteria, dependencies, `workflow_step`, and a `difficulty` rated the same way) except that its `workflow_ref` is the named workflow; plan the named workflow's steps inside it the same way. For a step you add, put the reason in that task's description. If a rating proves wrong, use the existing `difficulty_rerated` path.

## Delegated work

The parent owns the node, briefs the child (skill `agent-team-decomposition`), attaches it, verifies the return against the node's acceptance criteria, and settles the node. The child keeps its own plan and returns a result with evidence. If a return cannot be harvested, keep the node open, route the defect, and re-brief or wait. Reuse or stand-down of a finished worker is not promised; use what the delegation tools return.

If these steps are repeatedly skipped despite correct delivery, record the missing mechanical gate as a capability gap instead of adding prose.
