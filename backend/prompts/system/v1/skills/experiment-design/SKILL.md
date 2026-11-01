---
name: experiment-design
description: Use before designing, running, extending or reading out any experiment, benchmark, A/B or comparative evaluation (model, prompt, wording, tool, workflow, cost, performance); when recording, counting or tracking something your work does, being woken or acting each time something happens, or an alert or chart for a number; and for defining or interpreting a KPI. Not for regression tests or source inspection.
---

# Experiment design

Counting, tracking, being woken by or timing something your work does, or an alert or chart for a number, not testing a change? Go straight to `references/instrument-measures.md`. If they want to be told when something happens, tell them first that a rule wakes only you (or a temporary worker you started) and you then message them; it never wakes another persistent agent or them directly. The rest of this file is for experiments and comparisons.

An experiment exists to inform one decision at the lowest cost that can inform it. Spending past that point is waste, not rigor.

## 1. Set the bar from the consequence

Before anything runs, write the decision, the change being tested, its baseline, and what a wrong conclusion would cost. The confirmation bar matches that cost and never exceeds it.

| Wrong conclusion would be | Bar |
|---|---|
| Cheap to notice and undo (wording, prompt, rubric, config, a reversible default) | Direction check: the change is clearly better on a small sample. No perfect score, no significance test |
| Costly to undo, or it changes what many people or agents do | Consistent effect across a larger varied sample, plus a check of the main quality floor; someone other than the author grades the results |
| A published or general claim (superiority, savings, benchmark results, a persistent owner's readiness) | Controlled comparison per section 4, with uncertainty reported |

Reuse what already exists as the baseline (current outputs, historical labels, prior runs) instead of re-running it, when it was produced under the same conditions as the new arm apart from the change; otherwise re-run it or narrow the claim.

## 2. Stage it: every stage must earn the next

Plan the experiment as stages of growing size. Start with the fewest samples that could show the direction, and size each later stage by what a wrong conclusion would cost, never by a habitual number. Never launch the full size first. Before each stage, write three rules:

- **Stop, it works:** the signal already meets the bar from section 1. Stop and decide; more samples add cost, not confidence the decision needs.
- **Stop, it doesn't:** the signal is absent, weak, or pointing the wrong way. Stop, record it, and change the idea instead of buying more samples to rescue it.
- **Extend:** the signal is promising but short of the bar. Run the next stage only then.

After each stage, read the results, including failures and cost, and apply the rules as written. Do not move a threshold after seeing data. Do not rerun a stage hoping for a better number.

The consequence sets the size. Rewording a grading rubric you can revert tomorrow can stop once a handful of cases point clearly one way. A line every agent reads on every turn, or a price change customers will see, earns a larger and more varied stage first, because a wrong call repeats everywhere until someone notices.

## 3. Keep the stages honest

- Before reading any stage, confirm the change actually reached the actor in each arm (its delivered context or input), not just the brief.
- Inputs match what the real actor sees at that moment. Keep answers, labels and later information out of what is being tested.
- Change one thing between arms. If several things change together, claim only the combined change.
- Count every cost: setup, failures, retries, helpers and evaluation. Unknown cost is not zero. Set a spend ceiling for the whole experiment before stage one.
- A measure you define must answer a question its audience acts on. Prefer source data over self-grading, and say what the measure cannot see. To record measures of your own work, read `references/instrument-measures.md`.

## 4. Controls for high-consequence claims only

Use this section only for a published or general claim (section 1). Freeze the hypothesis, outcome, analysis and stopping rules before any results. List each factor that could differ between arms (model and version, harness, delivered instructions and context, data split, environment, cache, run order, scoring) and say how it is held fixed, randomized, or left uncontrolled with a narrower claim. Verify what was actually delivered to each arm, not what the brief said. Have someone independent check the design and the controls before the deciding stage.

## 5. Report

Report the stage reached and why it stopped, raw results with failures and exclusions, total spend, and the decision. A stop for weak signal is a valid result: record it so nobody repeats it.
