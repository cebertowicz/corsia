"""Updates their agent replaced after its check notice, and how long after the notice each came.

The notice is the line ``core.attention_notice`` appends to a saved update's result, asking the
agent to rewrite it before the person reads it. ``updates_replaced_after_notice_pct`` is the share of
updates shown that notice which their agent then replaced; only an unseen update can be replaced
(``infra.message_update_state.validate_targets``), so each of them was replaced before the person saw
it. The delay series report how long after the notice those replacements came, as the median and the
90th percentile of the period's delays. There is no cutoff: every replacement after the notice counts,
however late, and the spread shows how late.

Each delay travels as a ``(message id, seconds)`` pair in a frozenset, so the comparison windows and
moving averages, which join sets when they pool periods, read the pooled window's own spread.
"""
from __future__ import annotations

from typing import Any, Sequence

UPDATE_METRICS = frozenset({
    "updates_replaced_after_notice_pct",
    "update_replace_delay_median_seconds",
    "update_replace_delay_p90_seconds",
})

#: The quantile each delay series reads, as an exact fraction (numerator, denominator).
_QUANTILES = {"update_replace_delay_median_seconds": (1, 2), "update_replace_delay_p90_seconds": (9, 10)}


def nearest_rank(ordered: Sequence[float], numerator: int, denominator: int) -> float | None:
    """The nearest-rank quantile ``numerator/denominator`` of sorted values, or None for no values.

    Nearest rank returns a value that occurred, so "1 in 10 took X or more" stays a mechanical reading
    even in a short history, where interpolation would invent precision.
    """
    if not ordered:
        return None
    return ordered[max(0, (numerator * len(ordered) + denominator - 1) // denominator - 1)]


def update_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn each period's ``id:seconds`` list into the pair set the windows pool."""
    for row in rows:
        listed = row.pop("delays", None) or ""
        row["delays"] = frozenset(
            (message_id, int(seconds))
            for message_id, _, seconds in (item.rpartition(":") for item in listed.split(",") if item)
        )
    return rows


def update_value(metric: str, source: dict[str, Any]) -> tuple[float | int | None, int]:
    """One updates series' figure and the count it stands on."""
    if metric == "updates_replaced_after_notice_pct":
        n = int(source.get("n", 0) or 0)
        return (source.get("replaced_after_notice", 0) / n if n else None), n
    delays = sorted(seconds for _id, seconds in source.get("delays", frozenset()))
    return nearest_rank(delays, *_QUANTILES[metric]), len(delays)
