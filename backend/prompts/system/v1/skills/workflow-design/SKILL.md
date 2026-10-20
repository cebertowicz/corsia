---
name: workflow-design
description: Use when building or improving a workflow, the steps for one kind of recurring work, who does each and where the person is asked. Not for a one-off.
---

# Workflow design

A workflow is how one kind of recurring work gets done: the steps, who does each one, and where the person is asked. It is only for recurring work; a one-off is a plan, however big.

A new workflow starts when the person asks for one, or when the weekly review of their time (skill `time-to-agent`) finds them doing the same work by hand and they say yes to taking it over. Either way, this is the method.

## Method, in order

1. **Library first.** Look for a workflow or skills that fit, in the skills you have and the shared library (use what your tools list). Reuse or adapt one. If none fits, say why.
2. **Best practice.** Find how the best practitioners do this work, with evidence of their outcomes. Record credible alternatives, or call the process experimental if there are none; prestige alone is not evidence of fit. For each step, write what top quality looks like and where it fails, for people and for AI agents: losing track in long work, running out of room to hold what it needs, treating something it read as the person's approval, sounding sure when wrong. Write a check against each failure, then one open question that looks past the checks ("What would a top practitioner notice here that these checks miss?"). Whatever the question turns up becomes a check.
3. **The person's own examples**, and what you already know of how they work, from their sources, before you ask them for any. Ask only for what research and their examples cannot answer.
4. **The steps.** Fit the practice to the person's goal, scale, tools and budget, and explain important departures. Name each step's doer by role (the person, the agent that owns the work, a reviewer), never a specific agent. Mark where the person is asked, and the limits they set (what it may spend, who it may send to). Keep it no larger than the work needs.

Test the weakest assumption with the smallest pilot inside authority and cost limits, compare the result with what success means, and revise.

## Every run

- A run is one task in the plan, marked with its workflow (`workflow_ref`) when you add it; it can't be added later. Its steps are child tasks of it, each naming its `workflow_step`.
- The plan records which version of the workflow the run followed; you don't pass or restate it.
- It ends by checking the whole result against what went in, and says what happens to anything that fails that check.

## Save it

Save it as a skill (skill `skill-creator`), with its steps in the skill's `corsia.yaml`, so the shared library can carry it later. Write it to fit any project: name each limit by what it controls (a spending cap, the approved recipients) and keep the values, the person's examples and anything that identifies them in this project's notes, never in the skill.

## Improve it

- Judge a change by what happened: accepted or sent back, the person's corrections, cost, time. Never by your own check.
- A workflow with too few runs to judge, a monthly one say, is reported as not yet judged, never as working.
- When you improve a workflow, or a newer version arrives from the shared library, every place where it stops to ask the person and every limit they set stays as it is. Only the person removes them.
