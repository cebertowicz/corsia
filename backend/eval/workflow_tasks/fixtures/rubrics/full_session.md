# Rubric — Full-session workflow eval

The judge scores 0.0-1.0 over these criteria. Each criterion is a hard
requirement; partial satisfaction earns proportional credit.

## 1. Completion (weight 0.30)

Did the assistant respond to all three turns? Did the third turn
actually produce a one-sentence summary (not a long answer, not a
deflection, not a re-explanation)?

- 1.0: All three turns answered; turn-3 is one sentence.
- 0.5: All three turns answered but turn-3 is several sentences or
  is a non-summary.
- 0.0: One or more turns missing or empty.

## 2. Assistant-message correctness (weight 0.30)

Are the answers substantively right?

- Turn 1: Does the practical reason actually make sense (e.g. cost,
  latency, throughput, predictability)?
- Turn 2: Is the restatement plausibly clearer for a non-LLM-engineer?
- Turn 3: Does the summary reflect what was actually said earlier?

Score the average across the three turns. Make-up content or factual
contradictions are a 0.0 on the affected turn.

## 3. Token efficiency (weight 0.20)

No runaway loops. Reasonable iteration counts (a single-shot reply per
turn is fine; >5 iterations on a no-tool turn is a red flag).

Use `PER_TURN_ATTRS.iteration_count` when present:
- 1.0: every turn ≤ 2 iterations.
- 0.5: at least one turn 3-5 iterations.
- 0.0: any turn > 5 iterations OR a turn shows an error.

If `PER_TURN_ATTRS` is missing entirely, skip this criterion (treat as
1.0 — don't penalize for absent instrumentation).

## 4. Coherence across turns (weight 0.20)

Does the dialog actually feel like a conversation? Turn-2 should
reference the reason from turn-1; turn-3 should reference both. Pure
restarts that ignore context cap at 0.5.

## Output format

A single JSON object on one line:

```
{"score": 0.82, "reason": "all 3 turns answered, summary captured both prior points"}
```

Use the weighted sum (criterion × weight). Round to two decimals. Keep
the reason ≤ 200 chars.
