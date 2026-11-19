---
name: run-the-project
description: >-
  The conductor's daily loop: route every new ask, track problems to proven-fixed,
  read the project measures, re-order work across agents, review what waits on the
  person, and tell them only what needs them. Use at the project's set rhythm
  (daily by default) or whenever the person asks for a run. Not for one status
  question or for doing an agent's domain work.
metadata:
  corsia-data: corsia.yaml
  corsia-format: '1'
---

# Run the project

One run per rhythm. Read this project's standard for this workflow first (`WORKFLOW-run-the-project.md` in the project's standards folder, or the file its coordination model names): it holds the sources, pages, cadence and the skills each step uses. With none yet, copy `references/project-standards.md` there, fill each line you can read, and mark the rest not known. The loop reads and routes; agents do their own work from their own plans and never wait on it to start.

## Ground

Read, this run, not from memory or notes: the priorities; each agent's plan state; and everything the person said since the last run, on every channel the project standard lists (chat, comments and replies at least; mail or others where the project uses them). On a first run, read the last seven days. A paused plan, a stall (no live worker and no action on flagged work for over an hour) and an active plan are three different states; read which. Write down what changed since the last run, with sources. Name anything you could not read rather than guessing it.

## Asks

Each new ask becomes one complete task in its owner's plan: the person's words, the document or surface, the outcome wanted. Check it isn't already answered or tracked first. An ask with no owner, or whose owner is paused, is the conductor's. A correction on the conductor's own work is triaged and its outcome recorded; the fix goes where the mistake was made (a step, a standard, or the skill for that tool), never a new always-loaded line. A change of direction goes to every agent it affects, once. Comments on the conductor's documents and returns from its own workers are answered here.

## Problems

A problem is something that keeps going wrong, not one bug. New ones get a register row: what happened with evidence; the root cause, found by asking why until one owner can change it; that owner and their task; the fix; the evidence that will prove it fixed; when it's next checked. The row holds no status: read status from the owner's task. Check each row due today against its evidence. It closes only when that evidence shows up; a repeat reopens it. A fault in CORSIA itself goes to the CORSIA agent with its evidence.

## Measures

Read each project measure from its source, with its conditions. One moving the wrong way goes to Tell with its numbers. When a number leaves its normal range, act on it with movement-review. Never change a rule on a single reading. If the project has no measures yet, that is a finding: put a task to define them in your plan.

## Order

Rank work across agents against the priorities with skill `prioritization`, highest first; the order things arrived only breaks a true tie. State the cut. Update the live status page from the plans as read this run. For a stall, send its owner one message with the outcome wanted.

## Your plate

List everything waiting on the person. For each: does it matter now, and can they act on it from its top line? Drop what doesn't matter now and say where it went; fix what they can't act on (restate the goal, put the decision first, one screen). Never re-ask what they declined or left unanswered.

## Spot check

The steps above are checked by the agent that ran them, so a missed ask passes them. Hand this step to a fresh, cheap worker that ran none of them. It reads the project standard, this run's record and the person's raw messages and comments since the last run. It lists every ask, correction or waiting item the record missed, or marked tracked without saying where. Route each miss before Tell.

## Tell

At most one message, and only when something needs the person or is a deliverable they asked for. First line answers or states the result; then at most five short items in outcome terms; then the one thing you need. Calls made for them since the last message go in as one line each, with how to reverse it; they never make a message go on their own. If nothing needs them, send nothing and record why.

Before the run ends, schedule the next run. Give every item waiting on someone, the person included, a wake with a bound and the default you will take when it comes. A run that leaves open work with neither has not finished.

## How runs are judged

Read across runs, from the person's own messages and the plans: the person's asks and corrections per finished task (lower), items that reached them with nothing for them to decide or do (lower), problems closed because their proof showed up (higher), and uncached tokens per run (lower).
