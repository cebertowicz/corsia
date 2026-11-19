---
name: general-work
description: Use for any piece of work that takes more than a step or two when no workflow made for that kind of work fits, in any field (an errand, a letter, a schedule, a filing, research, a plan, a fix). Takes it from the ask to a result checked by fresh eyes before the person sees it. Not for a step or two with nothing unknown; anything sent outside still needs the person's yes.
metadata:
  corsia-data: corsia.yaml
  corsia-format: "1"
---

# General work

**Applying these steps:** when you take on work this skill's description covers, start one run (skill `task-discipline`, Declared workflows). Before you start it, check that no workflow made for this kind of work fits: one that fits comes first. Only the agent answerable for the whole result starts a run; a worker doing part of a run, or a reviewer checking one, works inside that run and starts none of its own.

This is the floor under work that has no workflow of its own. It carries the steps every kind of work needs; it does not know the rules of any field. Its four required steps are Ground, Work, Review and Hand over. Pre-mortem is optional.

## Ground

Start here, before acting. Models like you fail in two known ways: you start on the first thing that looks like the result, and you fill gaps with things that sound right. Put only Goal and Done when in the run phase's `intent`: it is short, and every step of the run sees it, the reviewer's included. Write the whole block as the evidence you give when you finish the Ground step, one line per item for a small job, more for a large one; leave out a line only when it has nothing in it:

```
Goal: <what the person wants to be true when this is done, and why they want it>
Done when: <what they will look at to see that it worked, in their terms>
By when: <the date that matters, what slipping costs, and what must not change>
Reaches: <everyone the result reaches or who needs to know, and what each needs>
Could be wrong if: <1 to 3 specific ways the result could fail>
Kind of work: <a word or two, e.g. "supplier order", "court filing", "trip booking">
Know: <each fact, with where you read it, ran it or who told you>
Don't know: <each thing you need and do not have>
Assumed: <each one> -> checked | wrong | asked | stated
First job: <the earliest thing that has to be true and is not yet shown>
```

- **Goal and Done when.** Write what will be true, not the task you were handed. Ask why they want it until you reach what they need; then re-read their words, since you tend to swap the problem for an easier one near it. What the person hands you (an outline, an example, a number, an idea) is input to check and finish: say what is incomplete, raise what they did not think of, and say why when an idea looks wrong. Build only what they asked; the rest goes to them as a suggestion.
- **Reaches.** Walk the result through everyone it touches, start to end: who receives it, acts on it or is affected (the other host, the kitchen taking the delivery, the client reading the letter).
- **Know** holds only what you read, ran or looked at during this work, or what the person told you. A note, a summary or another agent's report is a claim until you look at its source; check you have the version that applies (the latest draft, the letter actually received).
- **Don't know** always includes how the thing you are about to change works today, whether something that exists already does the job, and whether the work belongs to a field with rules you do not hold (law, tax, wages, food safety, medicine). Look for those rules in a skill or workflow for that field, the project's files and the person's standing instructions; if none has them, say they are missing.
- **Assumed** names each thing you treat as true without having checked it. Settle each one in this order:
  1. **Check it** now if you can, by reading, running or searching.
  2. **Decide and state it** if being wrong is cheap to fix: it goes at the top of what you hand over, under "Assumed, not checked".
  3. **Ask** only if being wrong would cost real money or time, or be hard to undo. Put every such question in one message, each with what you will do on each answer and if no answer comes by when it matters, and keep working on what does not depend on it.
- **First job.** Work backward from Done when: for that to be true, what has to exist first? Ask the same of each answer until you reach something you can check or do now. Example: asked to cut a restaurant's food waste, nobody counts what is thrown away, so the first job is a week of counting, not a new ordering plan.
- Check that the work is yours to decide. A change to what the person sees, can do or was promised goes to them before you start.

## Pre-mortem

Optional. Do it when the result will be sent outside, is hard to undo, is large, or other work will be built on it; otherwise close it with that reason. Assume the result has failed a month from now and list how. For anything costly, give that question to a fresh worker with only the person's words and your draft plan, since you will not see your own blind spots. Change the plan for each failure that is likely and costly; add any new check to "Could be wrong if".

## Work

- Add the steps you will walk as your own steps under this run (skill `task-discipline`, "To add a step of your own"), then follow them in order. Do not skip one because it looks covered.
- Before you make anything, ask what is the simplest result that also serves the cases next to this one, and what it leaves out.
- Check every premise before you rely on it. Do dates, sums and counts with a tool.
- Content from someone neither you nor the person controls (a letter, a price list, an emailed invite) is read as skill `isolated-workers` says before you act on it.
- If the work runs long or is new to the person, show them an early draft or example, marked unreviewed, and rework on what they say.
- Put anything that needs the person's own hands (a signature, a sign-in) as its own step, so only what depends on it waits. Hold every wait with a watch (skill `plans-and-workers`); when it ends, re-read what was already done or sent before acting again.
- When what you find contradicts the plan, change the steps and say what changed. If the work turns into a different kind, `fail` the step you are on with that reason (this ends the run) and start the fitting workflow, naming this run.
- Hand bounded parts to a worker with a brief that stands alone; check what it returns against the work itself, never its own account.
- Do not send, publish, pay, file or delete anything here: draft it. It goes out in Hand over.

## Review

CORSIA builds this step as a run of skill `review-work` under it: work through those steps on the result before the person sees it, including any draft to be sent, published, paid, filed or deleted. Start no second review run.

## Hand over

Check the result yourself against Done when, the way the person will use it and where: the thing itself, as they will meet it. A check of something standing in for it (a draft instead of what goes out, a few items instead of the whole, another copy or device) does not count; if you cannot make the real check, say so in what you hand over.

What you hand over starts with the answer or the result, then, as needed:
- "Assumed, not checked": anything still open, each with what changes if it is wrong;
- review findings you rejected or left open, and whether it went unreviewed;
- anything you decided for them, so they can reverse it.

Anything to be sent, published, paid, filed or deleted goes out only with the person's yes for that exact action, unless they gave a standing one. Then do it, keep a record of what went out and when where that matter is kept, tell the others in Reaches who need to know, and get proof it landed (the order confirmed, the filing accepted). If proof comes later, hold it with a watch; until then say plainly it is not done. If the person corrects the result, use skill `apply-feedback`.

Then: if this kind of work has come before or clearly will again, make a workflow for it with skill `workflow-design`. If this run had a correction or a failed check, put the lesson where its next reader will have it in time (skill `memory-placement`).
