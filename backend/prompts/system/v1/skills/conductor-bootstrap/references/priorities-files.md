# Priorities files

Use this when you write, review or suggest a change to a priorities file: at setup, when an agent is created, and in the weekly instruction review.

## Two levels

- **The project's file:** `context/PRIORITIES.md`. What the whole team puts first.
- **Each agent's file:** `PRIORITIES.md` in that agent's own `context/` folder (`.threads/<agent id>/context/`). Its goals, and its own lines where they differ from the project's.
- Every agent loads both on every turn, so keep each to a few short lines. On an install with no project, an agent's own file is its only one.

## What a file holds

- **Goals** (agent files only): each goal has a measure and a counter-measure, unless another agent is named as its counterweight.
  - A measure shows the goal is being met, whether or not anything records it yet. Say where it is read, or `not recorded yet`.
  - A counter-measure gets worse when the agent pushes the measure too hard. Example: "orders served" against "complaints per week".
  - A counterweight agent is one whose job is to check this agent's work, such as a reviewer of code. Name it in place of a counter-measure.
- **Now:** the problems to solve, in order. A line names a problem, never a task: tasks stay in plans.
- **Always:** kinds of work this agent may start without asking, only where that differs from the project's file.
- **Dates:** only dates the person set.
- **Waits** and **Goes first, whatever is above** (the project's file only): what does not start yet, and what jumps the order (something that stops a person or an agent working today).
- **Never in a file:** budgets, single incidents, or rules for one kind of work. A rule for one kind of work goes in that work's workflow or skill.

## How to fill one

- Every line traces to the person's words or a decision they made, or reads `Not set yet`. Never fill a gap with what seems likely.
- Use the person's words, not platform terms or jargon.
- Propose goals, measures and counter-measures in plain words for the person to confirm. Never ask them to come up with one.
- The person approves each file. A file they have not approved does not count.

## What it starts

An agent moves a task out of Later only when the task fits a line the person approved, and it names that line when it does. Until the person approves an agent's own file, only the project's file counts. Nothing else leaves Later. An agent with no Now line it can work on, in either file, stays asleep: it starts nothing on its own.

## Who changes it

- The person owns every priorities file. Agents, the lead agent included, only suggest changes and never edit one directly.
- Changes are rare. When a Now line's problem is solved, the agent that solved it suggests removing the line. The weekly instruction review also looks for stale lines and suggests removing them.
