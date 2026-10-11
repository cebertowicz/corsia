---
name: experiment-design
description: Use before designing, running, extending or reading out any experiment, benchmark, A/B or comparative evaluation (model, prompt, wording, tool, workflow, cost, performance), and when defining or interpreting a KPI to decide whether a change worked. Run it in stages that each must earn the next, with a confirmation bar matched to consequence. Not for regression tests, passive monitoring or source inspection.
corsia:
  handler:
    kind: instruction
---

# Experiment design

An experiment exists to inform one decision at the lowest cost that can inform it. Spending past that point is waste, not rigor.

## 1. Set the bar from the consequence

Before anything runs, write the decision, the change being tested, its baseline, and what a wrong conclusion would cost. The confirmation bar matches that cost and never exceeds it.

| Wrong conclusion would be | Bar |
|---|---|
| Cheap to notice and undo (wording, prompt, rubric, config, a reversible default) | Direction check: the change is clearly better on a small sample. No perfect score, no significance test |
| Costly to undo, or it changes what many people or agents do | Consistent effect across a larger varied sample, plus a check of the main quality floor |
| A published or general claim (superiority, savings, benchmark results) | Controlled comparison per section 4, with uncertainty reported |

Reuse what already exists as the baseline (current outputs, historical labels, prior runs) instead of re-running it.

## 2. Stage it: every stage must earn the next

Plan the experiment as stages of growing size, for example 5, then 20, then 50 samples. Never launch the full size first. Before each stage, write three rules:

- **Stop, it works:** the signal already meets the bar from section 1. Stop and decide; more samples add cost, not confidence the decision needs.
- **Stop, it doesn't:** the signal is absent, weak, or pointing the wrong way. Stop, record it, and change the idea instead of buying more samples to rescue it.
- **Extend:** the signal is promising but short of the bar. Run the next stage only then.

After each stage, read the results, including failures and cost, and apply the rules as written. Do not move a threshold after seeing data. Do not rerun a stage hoping for a better number.

A reversible change usually ends at the first or second stage: at most 20 samples, one run.

## 3. Keep the stages honest

- Inputs match what the real actor sees at that moment. Keep answers, labels and later information out of what is being tested.
- Change one thing between arms. If several things change together, claim only the combined change.
- Count every cost: setup, failures, retries, helpers and evaluation. Unknown cost is not zero. Set a spend ceiling for the whole experiment before stage one.
- A measure you define must answer a question its audience acts on. Prefer source data over self-grading, and say what the measure cannot see.

## 4. Controls for high-consequence claims only

Use this section only when section 1 puts the claim in the bottom row. Freeze the hypothesis, outcome, analysis and stopping rules before any results. List each factor that could differ between arms (model and version, harness, delivered instructions and context, data split, environment, cache, run order, scoring) and say how it is held fixed, randomized, or left uncontrolled with a narrower claim. Verify what was actually delivered to each arm, not what the brief said. Have someone independent check the design and the controls before the deciding stage.

## 5. Report

Report the stage reached and why it stopped, raw results with failures and exclusions, total spend, and the decision. A stop for weak signal is a valid result: record it so nobody repeats it.
