---
name: design-interrogation
description: Use when designing or reviewing an architecture, schema, memory or retrieval system, performance or scalability plan, or any design that is expensive to reverse — steel-man and dismantle the options, define the central tradeoff precisely, budget the critical path, name what breaks at scale, and find the weakest assumption and the first prototype to kill it. Not for small changes inside an approved design.
---

# Design interrogation

A design is finished when it has survived its own strongest opponent. Do not hedge: each argument must be strong enough to convince a senior engineer on its own. Record the interrogation in the RFC or design artifact; present it through `decision-brief`.

## Ground

Before arguing, establish: the current architecture and its owners (`ARCHITECTURE.md`, code, tests), the applicable engineering standards (the project's engineering standards and `shared/standards/DEVELOPMENT-DOCTRINE.md`), the safety invariants, and the real workload numbers today and at the planned horizon (rows, nodes, requests per second, payload sizes, concurrency, tenants, models). A design argued without workload numbers is an opinion; say so and name the cheapest measurement.

## Phase 1 — Steel-man, then dismantle

1. Make the strongest possible case for the leading design.
2. Make the strongest possible case against it, in favor of the simplest alternative that could work (often: the existing owner extended, a flat table with an index, a file, or nothing).
3. Name the specific workload or usage characteristics that tip the decision one way or the other. State which of those characteristics are measured and which are assumed.

## Phase 2 — The hard tradeoff

Assume the chosen design. Name its central tension (structured writes over unstructured inputs, consistency over availability, precision over recall, cache stability over freshness, one owner over parallel paths). Then design the mechanism that resolves it:

- the write or ingestion path: rule-based, model-powered, or hybrid, and the failure modes of each;
- conflict semantics, defined precisely: what wins when two facts disagree, how time and provenance are represented, how a retraction is recorded;
- uncertainty: how partial or low-confidence input is kept out of the trusted set without being lost;
- the schema or contract, shown in full, with every type, field, edge, and state justified — and, more importantly, what was deliberately not modeled and why.

## Phase 3 — The critical path

Walk the primary read, query, or request path step by step with a budget (milliseconds, tokens, or dollars) per step and a total. Cover: how the relevant subset is selected when the whole is large; how multiple signals are combined so none drowns the others; the cold-start case with no history; and what the caller sees when a step times out or fails.

## Phase 4 — What breaks at scale

Name three failure modes that small-scale tests are unlikely to surface but that will be serious at the planned horizon (typical sources: growth in data volume, concurrency, retention, cost, or drift over time). For each: describe it precisely, explain why ordinary tests would miss it, and propose a mitigation that does not require redesign. These are predictions; label them as such and say what early signal would confirm each. If none exists, say so; that is a finding about the design.

## Phase 5 — The meta question

- Name the single weakest assumption in the design and the cheapest prototype or measurement that would validate or kill it before production code. That prototype becomes the first plan node.
- If the same actor both maintains and consumes the system (an agent writing and reading its own memory, a reviewer grading its own output, a model curating its own context), describe the feedback loop, judge whether it is stabilizing or destabilizing, and name the circuit breakers.
- State what evidence would show, after shipping, that the design was wrong.

## Output

Record Phases 1–5 in an RFC in `shared/planning/` (or the project's design home) with: summary and scope; verified current architecture; proposed design and interaction contract; data model and API changes; the five phases above; tests and rollout; open items. An architecture decision record is the Phase 1–2 result plus the choice made; keep it in the RFC or the domain's decision log, not a separate document type. This is the Engineering stage of `sdlc-process`. Hand the user a `decision-brief`: the recommendation, the tipping characteristic, the weakest assumption and its kill test, and the one decision that remains, if any.

## Failure signals

This skill failed if a reviewer or the user finds an unexamined alternative, an undefined conflict rule, a critical-path step without a budget, or a scale failure the author did not name.
