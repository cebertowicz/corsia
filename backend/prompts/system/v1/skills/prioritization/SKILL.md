---
name: prioritization
description: Use when ranking candidate work — backlog or issue review, sprint or roadmap planning, "what next", accepting a new request, sequencing owners — score each item against the objectives with evidence, draw an explicit cut line, and recommend via skill `decision-brief`. Not for ordering steps inside one task.
corsia:
  handler:
    kind: instruction
---

# Prioritization

Rank against the mission, not enthusiasm. Every score names the objective it moves and its evidence.

## Ground

1. **Objectives and measures:** the charter or user-stated goals and standing risks. With none, the ranking is opinion; say so and get the objective first.
2. **Candidate set:** every input source and its cadence (issue tracker, requests, held work, open plan nodes, messages). Include *do nothing* and anything the user mentioned but never filed.
3. **Commitments and capacity:** current scope and its owner, the user's review bandwidth, provider and compute limits, and changes that invalidate shared context (batch them).
4. **Prior decisions:** do not re-rank what the owner already decided unless new evidence exists; say what changed.

## Score

First honor urgent safety/incident obligations, fixed deadlines, explicit user priorities and prerequisite dependencies; a score cannot override them. For the remaining meaningful choices, use one row per item. RICE (Reach × Impact × Confidence ÷ Effort) is an optional comparison aid, not permission to start work. Use a project's established rubric when present.

The following is an illustrative ordinal rubric, not measured probabilities; show assumptions/ranges and test whether uncertainty changes the ordering.

| Factor | Setting |
|---|---|
| Reach | Who is affected and how often; state the number or the basis |
| Impact | 3 moves a named objective or removes a standing risk; 2 clearly supports; 1 indirect; 0 serves none |
| Confidence | measured 1.0; current research 0.8; sound theory with adjacent support 0.5; opinion 0.2 |
| Effort | Work including verification and the user's review time; note irreversibility separately |

Add in words: **cost of not doing it** and **what it unlocks**.

## Roadmap

- **Now:** fits capacity, one accountable owner, acceptance evidence, a measurement signal.
- **Next:** ordered, with the trigger that promotes each.
- **Later:** parked with the condition that revives it.
- **Not doing:** with reasons.

Draw the cut line and show dependencies as edges. Decide ordering inside existing authority; surface only an unresolved material choice under skill `decide-or-raise`. Recommend one plan and name what you would cut first. An item serving no objective is named feature-theater, not silently dropped, and a ranking that ignores a standing risk says why.

## Output

Deliver via skill `decision-brief`: recommendation in a sentence, the ranked table, the cut line, what was cut and why, the one thing needed. The scored table lives in the plan or a shared document that the brief links. The skill failed if the user asks why an item ranks where it does, what objective it serves, or must choose an order that did not change the outcome.
