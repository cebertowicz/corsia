# Priorities files

Use this when you write, review or suggest a change to a priorities file: at setup, when an agent is created, and in the weekly instruction review.

## Two levels

- **The project's file:** `context/PRIORITIES.md`. What the whole team puts first.
- **Each agent's file:** `PRIORITIES.md` in that agent's own `context/` folder (`.threads/<agent id>/context/`). It never repeats a line from the project's file: when the agent's work serves one of the project's Now lines, its own Now reads `Not set yet`, unless the person gave that agent a different order.
- Every agent loads both on every turn, so keep each to a few short lines. On an install with no project, an agent's own file is its only one.

## What a file holds

- **Use line:** the file opens with this line, in italics, and keeps it: `*Use: when two pieces of work compete, the one higher under Now goes first. The person owns this file; agents suggest changes and never edit it.*` Name the person in place of "the person".
- **Now:** the problems to solve, in order. A line names a problem, never a task: tasks stay in plans.
- **Always:** kinds of work this agent may start without asking, only where that differs from the project's file.
- **Dates:** only dates the person set.
- **Waits** and **Goes first, whatever is above** (the project's file only): what does not start yet, and what jumps the order (something that stops a person or an agent working today).
- **Never in a file:** goals, budgets, single incidents, or rules for one kind of work. Goals are long-term and live with the project's or the agent's own description of its work; a priorities file says only what comes first now. A rule for one kind of work goes in that work's workflow or skill.

## How to fill one

- Every line traces to the person's words or a decision they made, or reads `Not set yet`. Never fill a gap with what seems likely.
- Use the person's words, not platform terms or jargon.
- The person approves each file. A file they have not approved does not count.

## What it starts

An agent moves a task out of Later only when the task fits a line the person approved, and it names that line when it does. Until the person approves an agent's own file, only the project's file counts. Nothing else leaves Later. An agent with no Now line it can work on, in either file, stays asleep: it starts nothing on its own.

## Who changes it

- The person owns every priorities file. Agents, the lead agent included, only suggest changes and never edit one directly.
- Changes are rare. When a Now line's problem is solved, the agent that solved it suggests removing the line. The weekly instruction review also looks for stale lines and suggests removing them.
