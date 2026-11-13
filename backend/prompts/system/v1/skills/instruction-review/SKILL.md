---
name: instruction-review
description: Use when the periodic line-by-line review of the instructions every agent always loads is due (weekly, or after a material roster or objective change), or when an addition would exceed the recorded size budget. Not for a wording fix inside the task you were asked to do, for documents and artifacts the person owns, or for a project's goals, commitments or decision rights.
metadata:
  corsia-data: corsia.yaml
  corsia-format: "1"
---

# Instruction review

**Applying these steps:** only when you run this review. Add one phase (`kind: phase`) with `workflow_ref: instruction-review`, as `task-discipline` describes; CORSIA builds a task per step. Every step here is `required`: finish it with evidence or `fail` it with the reason. Never let one drop quietly.

Improve agent behavior, not prose volume, and diagnose before you cut, per skill `context-stewardship`. Judge whether an agent would act rightly on its first move, not whether the file reads well.

**Always-loaded** means a source an agent carries before it has read the task: the scope's project instructions, each agent's own responsibility context, and whatever this install injects every turn. A skill or a workflow step is the opposite: it arrives only when triggered, so a rule put there must be reachable at the moment it is first needed. **Budget** is the limit on the part of that set written on this install; **headroom** is the share above the first count that the budget starts with.

Each step's section below reaches you when that step starts.

## Scope

Find the set the way `context-stewardship` says: the effective-context view if you have one, else the always-loaded files layer by layer. Context belonging to an agent that no longer exists is dead; a set nobody owns is unowned. List both as what they are instead of guessing. If no agent owns the set, review the part you may edit and say plainly which part has no owner.

## Measure

Count with something you can repeat: lines, or characters. Record the unit once beside the number, never change it between runs or mix units, and never guess a count. Count text that shipped with the product apart from the rest: nobody on this install can cut it, so it is reported beside the budget and never set against it.

The budget is the sum of three recorded numbers, and nothing else changes it: the first run's count of the text written on this install; the person's headroom on that count, a quarter by default; and the size of each addition the person has asked for since, so their own asks never cost a removal. A headroom the person sets later applies to that same first count. Never re-base the budget on today's count: a budget that moves with the set never limits it. What agents add on their own between reviews can take the set over the budget; this review brings it back under.

## Inventory

Quote each line as written; the quote is what the disposition and any before-and-after are built from, and a paraphrase cannot be applied by its owner. One file at a time: do not hold two always-loaded files open at once.

## Verdicts

Test each line with the questions in `context-stewardship`'s **Make each source earn its cost**; a line every recipient needs every turn stays. Only some agents need it: narrow it into the always-loaded context of the agents that do, where it still arrives in time. It is needed at a recognizable moment: condition it into a skill or workflow step whose trigger fires at that moment — a rule needed mid-task cannot live behind a trigger that only fires once the task is chosen. Prefer a workflow step when the moment falls inside work a workflow runs: the step names a section of that workflow's own skill, so the section arrives when the step starts, to whoever does it, no other agent carries it, and one workflow can replace several skills. That skill still loads by its description when an agent does the work without starting the workflow, so its description must name the moment, or the rule is lost off the run. An authority, safety or invariant line moves to a step only if that step is required. A rule several workflows need goes in one shared workflow each names as a step, never a copy in each. Nearly every agent needs it and one file happens to hold it: widen it. Delete only what no task needs; history someone still uses moves to a reference. With a single agent, narrowing has nowhere to go, so the choices are keep, condition, widen or delete. Keep an authority, safety or invariant line even when it is costly — move it, never drop it to save room (`context-stewardship`).

## Authority

Decide first whose words you may change. An invariant, an authority limit, or text the person wrote themselves goes to them as an exact before and after unless they have said you may edit it directly (see **Defaults**). A file another agent holds, and text that shipped with the product, always go to their owner as an exact before and after; you never edit them.

## Apply

Before applying any move or delete, prove the run's changes once as `context-stewardship`'s **Prove it** says. One coherent change per file. Write the destination first, then remove the original, so no rule exists nowhere partway through the run. A move that creates a skill or a workflow step follows `skill-creator`: the narrowest scope that triggers it, and a local copy named like a shipped skill has no effect. One home per rule: search the delivered layers first and amend the canonical source instead of adding a second copy (`context-stewardship`, `memory-placement`).

## Reread

Read the file as its first reader, on a fresh look, not as its editor. Knowing what you changed makes a dangling reference invisible; reading from the first line makes it visible.

## Whole result

Reconcile against your inventory, not your memory. Then ask what a careful auditor would notice that these checks miss, and answer that before you call the run done. A cut that shrinks the set but worsens behavior is reverted, as `context-stewardship` requires; the lagging checks on this step are how you find out, so record the review date where you will meet them again.

## Cadence

Arm the wake yourself; a horizon written only in a message wakes nothing. If nothing here can arm one, record that gap so the report can say it, instead of naming a date no one will meet.

## Report

Only when the set changed or something needs the person: say what changed in outcome words, in the shape skill `asking-the-person` gives: the size before and after, what moved where, what waits on another owner, and what wakes the next review. A person's goals, commitments and decision rights are never filed under obsolete guidance — when one looks stale, ask, don't edit.

## Defaults

Never ask the person about these; run each as written and record it once with the count. Size is counted in lines or characters, whichever you can count repeatedly. The budget's headroom is a quarter above the first count. You propose changes to the person's own instruction text and never edit it. The review runs weekly, and after any material roster or objective change. It stops at the always-loaded set. When the person asks, unprompted, for one of these to be different, that replaces the default and is recorded.

## Never

No new checker, gate, tool or background job: every action here is a read, an edit, a message, or a plan operation you already have.
