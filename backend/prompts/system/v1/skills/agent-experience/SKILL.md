---
name: agent-experience
description: Use when designing or reviewing anything an agent consumes or operates — tool schemas and errors, shells, permissions, context sources, bootloaders, skills and triggers, plan and event surfaces, briefs, prompts, CLI output. Map the agent's journey like a user's and verify on a fresh weak-model turn. Not for human-facing UI.
corsia:
  handler:
    kind: instruction
---

# Agent experience (AX)

Agents are a second user class. Most multi-agent failures are specification and coordination failures, not capability failures. Design the agent's journey as carefully as a user's.

## Ground

- What the agent actually receives for its role and harness: sources, order, size. If no delivery view is available, say so; file size is not delivery.
- The surface exactly as the agent sees it: schema, required fields, enums, error strings, return shapes.
- What the agent can read and write from its scope (workspace zones, leases, shared categories).
- The weakest model that must succeed: the least capable one your spawn policy allows, not the one you run on.

## Map the journey

One line per step:

| Step | Question |
|---|---|
| Trigger | What wakes the agent, and what does it know then? |
| First screen | Which sources, in what order, before it decides anything? |
| First decision | What must it decide on turn one, and does the context make the right call obvious? |
| Affordances | Which tools and fields does it reach for, and does the schema block the wrong call? |
| Failure states | For each error or empty result, does the text say what happened and what to do? |
| Done signal | How does it know it is complete, and where does evidence go? |
| Handoff | Can the next actor act on the result cold? |

## Rules

- **One concept, one name** across schema, prompt, docs, and errors.
- **Schema over prose.** A required field, enum, or validation beats a sentence to remember. Make the bad state unrepresentable before writing another instruction; a missing mechanism is a capability gap, not more text.
- **Errors are affordances:** cause, exact fix, valid alternatives. A bare refusal is a dead end.
- **Right slice, right time.** Always-loaded context carries only what changes a first-turn decision on nearly every turn. A trigger is precise enough that a weak model fires it on the right request and not its neighbor.
- **Loud, not silent.** A missing source, stale view, or unavailable tool is reported, never substituted, and no instruction may promise a capability the tools do not provide.
- **Retry-safe:** mutations take a stable key and expected revision.
- **Never require unreadable context:** an instruction pointing at a path, tool, or lease the agent cannot use is defective.

## Verify

Run a fresh delegated turn on the weakest supported model with three prompts: a positive case, a neighboring case that must not trigger, and a failure-shaped case (missing input, stale state, denied action). If that model is unavailable, use the cheapest you can dispatch and say which. Record what it received, its first decision, correction rounds, and size. A changed file is not behavioral evidence. If no supported fresh-turn dispatch or delivery inspection route is available, record the verification gap and do not claim the check passed.

Record the journey map and result in the work object; hand the user a brief (skill `decision-brief`) if a decision remains. The change failed if the same wrong first decision recurs, a user correction traces to the surface, or the weak-model check needed more than one correction.
