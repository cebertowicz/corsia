---
name: instruction-review
description: Use when the periodic line-by-line review of the instructions every agent always loads is due (weekly, or after a material roster or objective change), when an addition would exceed the recorded size budget, or when the person asks to cut instruction size. Not for a wording fix inside the task you were asked to do, for documents and artifacts the person owns, or for a project's goals, commitments or decision rights.
metadata:
  corsia-data: corsia.yaml
  corsia-format: "1"
---

# Instruction review

**Applying these steps:** only when you run this review. Add one phase (`kind: phase`) with `workflow_ref: instruction-review`, as `task-discipline` describes; CORSIA builds a task per step. Every step here is `required`: finish it with evidence or `fail` it with the reason. Never let one drop quietly.

Improve agent behavior, not prose volume, and diagnose before you cut, per skill `context-stewardship`. Judge whether an agent would act rightly on its first move, not whether the file reads well.

**Always-loaded** means a source an agent carries before it has read the task: the scope's project instructions, each agent's own responsibility context, and whatever this install injects every turn. A skill or a workflow step is the opposite: it arrives only when triggered, so a rule put there must be reachable at the moment it is first needed. **Budget** is one recorded number for the always-loaded set; **headroom** is what that number leaves above today's count.

## What each step needs

- **scope** — Find the set the way `context-stewardship` says: the effective-context view if you have one, else the always-loaded files layer by layer. Context belonging to an agent that no longer exists is dead; a set nobody owns is unowned. List both as what they are instead of guessing. If no agent owns the set, review the part you may edit and say plainly which part has no owner.
- **measure** — Count with something you can repeat: lines, or characters. Record the unit once beside the number, never change it between runs or mix units, and never guess a count. A budget set at today's count plus the edit in front of you is not a budget: the set then freezes at its current size, and every later addition — including the one the person just asked for — costs a removal. Set or raise the number before you judge any line.
- **inventory** — Quote each line as written; the quote is what the disposition and any before-and-after are built from, and a paraphrase cannot be applied by its owner. One file at a time: do not hold two always-loaded files open at once.
- **verdicts** — The keep test is one question: does every agent that loads this need it before it can make its first correct move? Yes, keep. Only some agents need it: narrow it into the always-loaded context of the agents that do, where it still arrives in time. It is needed at a recognizable moment: condition it into a skill or workflow step whose trigger fires at that moment — a rule needed mid-task cannot live behind a trigger that only fires once the task is chosen. Nearly every agent needs it and one file happens to hold it: widen it. Delete only what no task needs. With a single agent, narrowing has nowhere to go, so the choices are keep, condition, widen or delete.
- **authority** — Decide first whose words you may change. An invariant, an authority limit, or text the person wrote themselves goes to them as an exact before and after unless they have said you may edit it directly (see **Choices the owner sets**). A file another agent holds, and text that shipped with the product, always go to their owner as an exact before and after; you never edit them.
- **apply** — One coherent change per file. Write the destination first, then remove the original, so no rule exists nowhere partway through the run. A move that creates a skill or a workflow step follows `skill-creator`: the narrowest scope that triggers it, and a local copy named like a shipped skill has no effect.
- **reread** — Read the file as its first reader, on a fresh look, not as its editor. Knowing what you changed makes a dangling reference invisible; reading from the first line makes it visible.
- **whole-result** — Reconcile against your inventory, not your memory. Then ask what a careful auditor would notice that these checks miss, and answer that before you call the run done. A cut that shrinks the set but worsens behavior is reverted, as `context-stewardship` requires; the lagging checks on this step are how you find out, so record the review date where you will meet them again.
- **cadence** — Arm the wake yourself; a horizon written only in a message wakes nothing. If nothing here can arm one, record that gap so the report can say it, instead of naming a date no one will meet.
- **report** — Only when the set changed, something needs the person, or one of their choices is still unanswered: say what changed in outcome words, in the shape skill `asking-the-person` gives: the size before and after, what moved where, what waits on another owner, and what wakes the next review. A person's goals, commitments and decision rights are never filed under obsolete guidance — when one looks stale, ask, don't edit.

## Choices the owner sets

Ask once, record the answer, and run the default in brackets until they answer: how size is counted [lines or characters — whichever you can count repeatedly]; the headroom the budget must leave above today's count [a quarter above it]; whether you may edit the person's own instruction text or only propose it [propose]; the review horizon [weekly, and after any material roster or objective change]; whether the review reaches past the always-loaded set [no]. Each open choice appears in that run's report; their answer replaces the default and is recorded.

## Never

No new checker, gate, tool or background job: every action here is a read, an edit, a message, or a plan operation you already have. Keep an authority, safety or invariant line even when it is costly — move it, never drop it to save room (`context-stewardship`). One home per rule: search the delivered layers first and amend the canonical source instead of adding a second copy (`context-stewardship`, `memory-placement`).
