---
name: product-thinking
description: Use before building, proposing, scoping, designing, or approving any feature, product, service, or process change (including an imperative "build X" or "add X"), any requirements document, and when a user reports a pain point — run the product chain and hand off via skill `decision-brief`. Not for executing an already-specified change.
---

# Product thinking

The owner's attention is the bottleneck. The chain is the homework that lets them decide instead of co-design. Address each applicable part proportionately in the work object and present the decision-ready result. A small change may need only a few sentences; retain the evidence behind them.

## Ground

Read before forming an opinion:

1. Objectives and measures: the charter and any vision it names. If no charter exists, derive the objective from the user's instruction and resolve material ambiguity before designing.
2. The contract for the surface: the project's product, design, and engineering standards and requirements template where the charter places them. For agent-facing surfaces (tools, context, skills, plans, prompts, CLI output) skill `agent-experience` is that contract. Use the actual source owner's standards where present; do not require absent templates or assume authority to contact maintainers.
3. What exists: the composed current surface, current requirements, open issues, knowledge, prior decisions. Confirm the need is not met or rejected already.

Separate known, assumed, and open; an unverified claim is a hypothesis with a named cheapest check.

## The chain

Cover the decision-relevant questions; mark an inapplicable item with its reason rather than inventing content.

1. **Objective:** `Serves: <objective> because <clause>.` If none, stop: feature-theater. If the ask conflicts with the objective, say so and still deliver the analysis.
2. **Problem, not feature:** who has the job, where it fails, and the evidence, labeling observation versus inference. Keep the problem concise.
3. **Cost of the pain:** attention events, minutes, trust, money, missed work. Estimate and name the cheapest measurement if unmeasured.
4. **Alternatives:** consider doing nothing, using an existing solution and the proposed change where meaningful. Steel-man and challenge the credible alternatives. Name the characteristic that tips the choice.
5. **Constraints and invariants:** every standing rule the proposal touches (tagged invariants, safety rules, charter constraints) and how each is preserved.
6. **Dependencies and data:** what must exist first, its owner, and whether it blocks or only sequences.
7. **Pre-mortem:** at a relevant future horizon, how could the proposal fail, evade verification and harm the outcome? Name mitigations. Name the weakest assumption and the cheapest check that could kill it.
8. **Requirements:** functional (stable IDs, one testable obligation each) and non-functional as they apply: latency and scale, privacy and data control, offline behavior, accessibility, observability, what the user sees on failure, and operating cost (model tier, calls per run, retry multipliers, fees; not the narrower context-size count in skill `context-stewardship`). Explicit non-goals and concepts deliberately not introduced.
9. **Smallest slice:** what proves the value, what is deferred, and the trigger that resumes each piece.
10. **Score:** Reach, Impact, Confidence, Effort including approval and review. When competing with other work, use skill `prioritization`.

## Output

Record the chain in the owning plan node or a linked document, and present it via skill `decision-brief`: answer first, options only if a real decision remains, one thing needed. The skill failed if the user must ask what problem this solves, which objective it serves, what it costs, what the alternatives were, or what could go wrong.
