---
name: plans-and-workers
description: Use for multistep work, plan state, waiting, selecting a model or delegating a step to a worker.
---

# Plans and workers

## Live work

Intent is the authorized outcome; the plan sequences it; work state records what is complete, active, waiting and next. The actor-bound structured plan is canonical live state. Every actor, including temporary workers, uses it for multistep work or multiple tasks. Only one atomic, safely completed step may omit it. Workspace, project, role or thread context cannot waive it.

Read the current plan before relying on it. Give actionable nodes plain titles and acceptance criteria; use each operation for its actual meaning. Update state when accepted work, intent, evidence, dependencies, delegation or user direction changes. A refinement edits the existing outcome; a separate deliverable gets a node. Keep all requested stages visible until accepted, failed, or closed with a reason.

A plan mutation must produce the intended durable state. If the supported tools cannot do that, report the capability failure; never substitute `PLAN.md` or an empty acknowledgement. Use a dependency for a supported node prerequisite and a deadline watch for an external wait, let it carry the wait instead of re-reading status, then continue the next ready task. Follow the tool's actual scope and notification contract; never invent cross-owner access or a wake guarantee.

## Delegation and collaboration

Sort each step before starting it:

- **Judgment stays with the accountable owner:** intent, acceptance against it, refuting a reviewer's block, scope, product and design calls, anything the user decides, the analysis behind a recommendation (its options, trade-offs and write-up) and the tightly coupled reasoning that feeds them. Brief workers with the result instead.
- **Execution defaults to a suitably capable subagent:** implementation, debugging, evidence gathering, document work, fixing what a check found and any other self-contained step whose result can be checked. Hand off a step likely to take more than about 10 tool calls, and each time a turn passes about 25, check whether the rest should be handed off; a workspace spawn policy may tune these starting values. Keep a step inline only when no suitable worker is available or a self-contained brief and independent verification would cost more than doing it.
- **Procedural follow-through goes to a mechanism first:** a watch, dependency, event subscription or script; otherwise it stays with the owner. Run a ready, known command (a fixed script or check) yourself, in the background where supported so its exit wakes you; never relay it through a worker.

Every deliverable has one accountable agent, even across several areas: the one the request went to, unless the person, the conductor or project instructions name another. Others advise or do their parts; it delivers, checks and closes. A task you hand out stays attached in your plan until you verify its evidence and settle it; a child's turn ending is not completion.

Coordinate on durable work objects; keep status there instead of pushing routine messages. Comments and registration imply notification only where the supported integration guarantees it. Peer assignments require a direct user grant covering that assignment; a commissioning grant may cover a defined class, but never expands itself. Temporary workers remain inside the parent's authorization.

Use a peer interrupt only for a concrete necessary action, missing input or actionable failure when no sufficient shared or event-driven route exists. No acknowledgements, FYIs or duplicate completion notices. Parent-child briefs and returns remain required.

Reach several agents with one shared notice at their common scope, which they receive on their next turn (its conductor, or the CORSIA agent at workspace scope, places it; anyone else proposes the exact text, evidence and expiry to them), never one message per agent: every message wakes a turn and spends capacity. Message one owner directly only when they must act before their next natural wake.

## Worker selection

Choose by task fit, consequence, required tools, capacity and cost within the workspace's allowed targets. Discover current selectors and capabilities from supported surfaces; never require an absent local policy file or guess model aliases. Model choice grants no authority and replaces no verification.

Use the least costly tier that can reliably perform the work: mechanical for clear transformations; a standard all-rounder for bounded execution or judgment; stronger reasoning for substantial ambiguity, interacting systems, difficult-to-detect failures, high consequence or a failed standard-tier attempt. Length alone is not complexity. Consider total cost per accepted outcome, including rework.

Before dispatching a stronger-reasoning worker, write one line in its brief naming the criterion the work meets and why a standard all-rounder will not do; without that line, use the standard tier. Apply the same threshold whatever model the parent uses. Verify the selected harness has the required tools, permissions and modalities; model capability alone proves none of those. Keep long execution bounded and delegate when it preserves responsiveness.

Read this instance's model catalog (`GET /api/models`) before dispatch and use its exact `litellm_model`. Choose permitted `ready` rows; `available` alone does not establish readiness. Read `status_reason` and any reset time for unavailable/usage-limited rows. Catalog capabilities describe the model, not the harness's complete tool surface. Missing quota or utilization data is unknown, never spare capacity. Respect workspace budgets and provider terms. Do not load or switch local models without authority. Shared concurrency limits are not reserved capacity.

Pass an explicit exact permitted selector. Require accepted dispatch and an actual assistant turn. If unavailable, limited or failed, record the failure, then choose an allowed alternative or wait; no silent fallback. Inspect a quiet child's binding and last-turn error before calling it idle. Recovery must use supported tools and preserve the task's return obligation.
