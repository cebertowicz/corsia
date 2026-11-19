---
name: keep-the-set
description: >-
  The conductor's weekly upkeep of the project's set: its workflows, standards,
  skills, always-loaded instructions, agents and measures. Finds overlap, stale
  items, rules without one home, work done outside workflows, and failures a
  cheaper earlier check would have caught; decides, makes and reviews each change.
  Use weekly, after a roster or objective change, or when the person asks. Not for
  a wording fix inside another task.
metadata:
  corsia-data: corsia.yaml
  corsia-format: '1'
---

# Keep the set

One run a week. Read this project's standard for this workflow first. With none yet, copy `references/project-standards.md` into the project's standards, fill each line you can read, and mark the rest not known. The conductor owns patterns across workflows unless the coordination model names another owner. Each owner changes its own files; the conductor changes only its own and sends the rest as tasks.

## List the set

From source, not memory: every workflow (owner, standard, where work enters), every skill, the instruction files every turn loads (with byte size per role), the agents and their charters, and the project measures. A workflow with no owner or no standard is already a finding.

## Find what's wrong

Look for each kind and write 'none found' when there is none:
- two items doing one job, or near-copies of each other;
- items nothing used this week, or that describe a state that's gone;
- a rule with no home, or with two;
- recurring work done outside any workflow;
- failures this week from every workflow and from the final review: for each, the earliest step that could have caught it cheaply;
- always-loaded lines (skill `instruction-review`) and lessons that cross agents (skill `context-stewardship`).

## Decide the changes

For each finding: change it, route it to its owner, or leave it and say why. A failure caught late becomes a check at the earliest step that could catch it, written into that workflow's standard and step, never a new always-loaded line. Premortem each change: how it fails, what it costs to carry every turn or run, and the simpler option you weighed. What the person decides (what they see, authority, the roster, shipped instructions) is marked for them, not made.

## Make the changes

Make your own changes with skills `workflow-design` and `skill-creator`; send each other owner one complete task. Every changed workflow or skill passes the platform's validator before it counts.

## Fresh-eyes review

A reviewer that did none of this week's work reads each change against its finding and premortem, and reads the daily loop's evidence for steps passed without being done. Fix or answer each objection.

## List what changed

One dated list: each change, why, and how to undo it. It goes where the project keeps decisions made for the person, and each change becomes one line in the next daily Tell. Nothing goes to the person to review. A change the project reserves for them (its who-decides list) goes as one decision with your pick, never made. Schedule the next weekly run before you finish.

## How runs are judged

Read across runs: failures of a kind already fixed before (lower), bytes every turn loads per role, against their own history (lower), the share of finished tasks of an hour or more that ran under a workflow (higher), and uncached tokens per run (lower).
