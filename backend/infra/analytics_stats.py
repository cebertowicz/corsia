"""Statistics helpers for product analytics queries."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Stats:
    n: int
    mean: float | None
    median: float | None
    p95: float | None
    sd: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": self.mean,
            "median": self.median,
            "p95": self.p95,
            "sd": self.sd,
        }


def stats_for_values(values: list[float]) -> Stats:
    n = len(values)
    if n == 0:
        return Stats(0, None, None, None, None)
    mean = sum(values) / n
    if n == 1:
        return Stats(1, mean, mean, mean, 0.0)
    sorted_v = sorted(values)
    mid = n // 2
    median = sorted_v[mid] if n % 2 == 1 else (sorted_v[mid - 1] + sorted_v[mid]) / 2
    idx = 0.95 * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    p95 = sorted_v[lo] + (idx - lo) * (sorted_v[hi] - sorted_v[lo])
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    sd = math.sqrt(var)
    return Stats(n, mean, median, p95, sd)


def ttest_summary(a: list[float], b: list[float]) -> dict[str, float | None]:
    if len(a) < 2 or len(b) < 2:
        return {"t": None, "p": None, "effect_size": None}
    try:
        from scipy import stats

        result = stats.ttest_ind(a, b, equal_var=False)
        t_stat = float(result.statistic)
        p_value = float(result.pvalue)
    except Exception as exc:  # noqa: BLE001
        log.warning("ttest_ind failed (returning nulls): %s", exc)
        return {"t": None, "p": None, "effect_size": None}

    sa = stats_for_values(a).sd or 0.0
    sb = stats_for_values(b).sd or 0.0
    na, nb = len(a), len(b)
    pooled_var = ((na - 1) * sa**2 + (nb - 1) * sb**2) / (na + nb - 2)
    pooled_sd = math.sqrt(pooled_var) if pooled_var > 0 else 0.0
    if pooled_sd == 0:
        effect = 0.0
    else:
        effect = (sum(a) / na - sum(b) / nb) / pooled_sd
    return {"t": t_stat, "p": p_value, "effect_size": effect}
