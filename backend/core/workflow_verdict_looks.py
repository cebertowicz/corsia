"""The planned looks: O'Brien-Fleming per-look confidence and the minimum detectable change (DECISION-LOG 2026-10-30).

A verdict looks three times, when each half holds 1, 2 and 4 times the plan's minimum (20, 40 and 80 runs by
default). Each look is judged at its own confidence, so the trial's level holds over all three; the minimum
detectable change tells the owner what size of change each look could have seen. It is shown, never read by a rule.
"""
from __future__ import annotations

from datetime import datetime
from math import sqrt
from statistics import NormalDist
from typing import Mapping, Sequence

from core.workflow_verdicts import (
    DEFAULT_CONFIDENCE, PlannedMeasure, Run, baseline_spread,
)

# Each look's multiple of the plan's minimum per half; its information fraction is the multiple over the last's.
LOOK_MULTIPLES = {1: 1, 2: 2, 3: 4}
POWER = 0.8
# O'Brien-Fleming: look k is judged at z = c / sqrt(t_k), t_k the information fraction (1/4, 1/2, 1), so its
# confidence is 2 * Phi(c / sqrt(t_k)) - 1. c is the constant at which the chance that any of the three looks
# excludes zero on a version that changed nothing equals one minus the trial's confidence.
# 0.8: from the decision log (400k simulated null trials; exact family rate 0.1990 at this c).
# 0.9 and 0.95 are the raised confidences. They are solved exactly, not simulated: the looks' statistics are
# Brownian motion at t = 1/4, 1/2, 1, so the no-crossing chance is the trivariate normal probability of
# |B(t_k)| < c at all three (scipy multivariate_normal.cdf, abseps and releps 1e-9), and c is found by bisection
# on 1 - that = 1 - confidence. The same solve gives c = 1.3478 at 0.8, whose family rate is 0.2000.
# backend/tests/test_workflow_verdicts.py re-derives all three.
OBRIEN_FLEMING = {DEFAULT_CONFIDENCE: 1.3504, 0.9: 1.6789, 0.95: 1.9776}


def look_confidence(confidence: float, look: int) -> float:
    """The confidence one look is judged at, from the trial's overall ``confidence``."""
    if confidence not in OBRIEN_FLEMING:
        raise ValueError(f"no O'Brien-Fleming constant for confidence {confidence}")
    fraction = LOOK_MULTIPLES[look] / LOOK_MULTIPLES[len(LOOK_MULTIPLES)]
    return 2 * NormalDist().cdf(OBRIEN_FLEMING[confidence] / sqrt(fraction)) - 1


def minimum_detectable_change(confidence: float, look: int, spread: float | None, minimum: int) -> float | None:
    """The smallest difference look ``look`` would call with 80% power: (z(1-a/2) + z(0.8)) * s * sqrt(2/n).

    ``a`` is the look's level (one minus its confidence), ``s`` the baseline's per-run spread and ``n`` the runs per
    half at the look. None, never 0, without a spread.
    """
    if spread is None:
        return None
    normal = NormalDist()
    spent = 1 - look_confidence(confidence, look)
    runs = LOOK_MULTIPLES[look] * minimum
    return float(f"{(normal.inv_cdf(1 - spent / 2) + normal.inv_cdf(POWER)) * spread * sqrt(2 / runs):.6g}")


def planned_mdc(measures: Sequence[PlannedMeasure], baseline: Sequence[Run], minimum: int, confidence: float,
                now: datetime) -> Mapping[str, tuple[float | None, ...]]:
    """Each deciding measure's minimum detectable change at every look, from the baseline arm at freeze."""
    planned: dict[str, tuple[float | None, ...]] = {}
    for measure in measures:
        if measure.role != "shown":
            spread = baseline_spread(measure, baseline, now)
            planned[measure.name] = tuple(minimum_detectable_change(confidence, look, spread, minimum)
                                          for look in LOOK_MULTIPLES)
    return planned
