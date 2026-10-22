---
name: workflow-design
description: Use when making or improving a workflow, the steps for one kind of work you will do again, who does each and where the person is asked. Not for a one-off or work done in a step or two.
---

# Workflow design

A workflow is how one kind of work gets done each time it comes back, at any interval: the steps, who does each one, and where the person is asked. Work done in a step or two gets none. A one-off that is not a kind of anything is a plan, however big; most big jobs are a kind (a design doc, a PR review) and get one.

Make one yourself when you will do a kind of work again and none fits (skill `task-discipline`), or when the person asks. Nobody approves it. Tell the person in one line inside your next message, never as a message of its own: `I'll handle these the same way each time, so I can tell what works. You can see or change the steps any time.` Taking work off the person's own plate is different: that needs their yes, through the weekly review of their time (skill `time-to-agent`). Each way, this is the method.

## Method, in order

1. **Library first.** Look for a workflow or skills that fit, in the skills you have and any shared library your tools reach. Adapt the fitting one with the best results. If none fits, say why. A script or connection it brings that reaches something you could not reach before still needs the person's usual yes.
2. **Best practice.** Find how the best practitioners do this work, with evidence of their outcomes. Record credible alternatives, or call the process experimental if there are none; prestige alone is not evidence of fit. For each step, write what top quality looks like and where it fails, for people and for AI agents: losing track in long work, running out of room to hold what it needs, treating something it read as the person's approval, sounding sure when wrong. Write a check against each failure, then one open question that looks past the checks ("What would a top practitioner notice here that these checks miss?"). Whatever the question turns up becomes a check.
3. **The person's own examples**, and what you already know of how they work, from their sources. Note what research and their examples cannot answer; that is all you ask them, after step 5.
4. **The steps.** Fit the practice to the person's goal, scale, tools and budget, and explain important departures. Name each step's doer by role (the person, the agent that owns the work, a reviewer), never a specific agent. Mark where the person is asked, and the limits they set (what it may spend, who it may send to). Keep it no larger than the work needs.
5. **Save it now** in your own skills (skill `skill-creator`), with its steps in the skill's `corsia.yaml`, before you ask the person anything or pilot it. Until their answers and the pilot revise it, it runs only as that pilot. A document in your thread folder is not a workflow: later runs never receive it. Write it to fit any project: name each limit by what it controls (a spending cap, the approved recipients) and keep the values, the person's examples and anything that identifies them in this project's notes, never in the skill.

Test the weakest assumption with the smallest pilot inside authority and cost limits, compare the result with what success means, and revise.

## Every run

- Plan each run as skill `task-discipline` says under Declared workflows.
- It ends by checking the whole result against what went in, and says what happens to anything that fails that check.

## Improve it

- Judge a change by what happened: accepted or sent back, the person's corrections, cost, time. Never by your own check.
- A workflow with too few runs to judge, a monthly one say, is reported as not yet judged, never as working.
- Merge workflows that turn out to cover the same kind of work, and retire one whose work has stopped coming back, judged against how often it usually comes.
- When you improve a workflow, or a newer version of it arrives, every place where it stops to ask the person and every limit they set stays as it is. Only the person removes them.
