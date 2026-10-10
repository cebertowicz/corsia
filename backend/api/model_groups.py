"""One owner for grouping per-model rows into named rows and one remainder row.

Models' table, Models' token share and Spend's By model all group here, so a
remainder row's membership, its counts and its cost class follow one rule set
instead of one per surface. Each section states its own membership in the
remainder's label, and no two share the bare word "Other":

* the Models table keeps models that spent something or have at least ``MIN_TASKS``
  accepted tasks, ranks them by priced spend, shows the top ``MODEL_ROWS`` and puts
  the rest in "Other (N more models)";
* token share and Spend keep their top ``SHOWN`` by their own measure and put
  the rest in "N more models".

A remainder counts tasks by distinct task id, so a task that used two of its
models counts once, and its rate divides by those distinct tasks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

#: Accepted tasks a model needs in the latest period for its own table row (and in
#: the previous period for a change); the rest share the floor group.
MIN_TASKS = 14
#: Named rows the Models table shows before its "Other (N more models)" row.
MODEL_ROWS = 6
#: Named rows a ranked section (token share, Spend) shows before "N more models".
SHOWN = 7
#: The cost outcome of a call on a model with no list price (a local model).
LOCAL = "not_api_priced"
#: Spend's per-row stored class, read as the call outcome it stands for.
_SPEND_OUTCOME = {"priced": "priced", "local": LOCAL, "no_pricing": "no_pricing"}


def cost_class(outcomes: Iterable[str]) -> str | None:
    """The one cost class for a model or a group of models, from its call outcomes.

    ``no_pricing`` means an API-priceable call has no price, so a figure covers
    priced calls only. Local calls have no list price by design: priced plus
    local is ``priced`` (``includes_local`` discloses it), never ``no_pricing``.
    """
    seen = set(outcomes)
    if not seen:
        return None
    if seen == {LOCAL}:
        return "local"
    return "priced" if seen <= {"priced", LOCAL} else "no_pricing"


def includes_local(outcomes: Iterable[str]) -> bool:
    """Whether a group that is not wholly local mixes in local calls."""
    seen = set(outcomes)
    return LOCAL in seen and seen != {LOCAL}


def other_label(count: int) -> str:
    """The Models table's remainder: the models past its named rows, counted."""
    return f"Other ({rest_label(count)})"


def rest_label(count: int) -> str:
    """A ranked section's remainder, named by how many models it holds."""
    return f"{count} more model{'' if count == 1 else 's'}"


@dataclass
class PeriodTasks:
    """One period of accepted-task cost for a model or group, keyed by task id."""

    tasks: set[str] = field(default_factory=set)
    #: Tasks with at least one call on an API-priceable (not local) model.
    priceable: set[str] = field(default_factory=set)
    #: Tasks with a call that failed to reach the ledger.
    gaps: set[str] = field(default_factory=set)
    dollars: float = 0.0
    outcomes: set[str] = field(default_factory=set)

    def add_call(self, task_id: str, outcome: str, dollars: float | None, gap: bool) -> None:
        """Count one classified usage row of ``task_id``."""
        self.tasks.add(task_id)
        self.outcomes.add(outcome)
        if outcome != LOCAL:
            self.priceable.add(task_id)
        if gap:
            self.gaps.add(task_id)
        if outcome == "priced" and dollars is not None:
            self.dollars += float(dollars)

    def merge(self, other: PeriodTasks) -> None:
        """Union another model's period in: a shared task counts once."""
        self.tasks |= other.tasks
        self.priceable |= other.priceable
        self.gaps |= other.gaps
        self.dollars += other.dollars
        self.outcomes |= other.outcomes

    def rate(self) -> float | None:
        """Priced dollars per distinct API-priceable task, None when nothing was priced.

        Local calls carry no dollars and their tasks leave the denominator, so a
        local model never dilutes a priced rate.
        """
        return self.dollars / len(self.priceable) if self.priceable and "priced" in self.outcomes else None


@dataclass
class ModelTally:
    """Everything the Models table reads for one model identity or group."""

    latest: PeriodTasks = field(default_factory=PeriodTasks)
    previous: PeriodTasks = field(default_factory=PeriodTasks)
    turns: int = 0
    failed: int = 0

    def merge(self, other: ModelTally) -> None:
        self.latest.merge(other.latest)
        self.previous.merge(other.previous)
        self.turns += other.turns
        self.failed += other.failed


def split_by_spend(
    tallies: Mapping[str, ModelTally], minimum: int = MIN_TASKS, shown: int = MODEL_ROWS,
) -> tuple[list[str], list[str]]:
    """Keys that hold their own table row, and the keys of the models pooled into "Other".

    A model is listed when it spent something in the latest window or has ``minimum`` accepted
    tasks; the listed models rank by priced spend, then tasks, and the first ``shown`` keep a row.
    """
    listed = [key for key, tally in tallies.items() if tally.latest.dollars > 0 or len(tally.latest.tasks) >= minimum]
    listed.sort(key=lambda key: (-tallies[key].latest.dollars, -len(tallies[key].latest.tasks), key))
    return listed[:shown], listed[shown:]


def _split_ranked(rows: list[dict[str, Any]], shown: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (rows, []) if len(rows) <= shown else (rows[:shown], rows[shown:])


def group_shares(
    shares: list[dict[str, Any]], shown: int = SHOWN, label: Callable[[int], str] = rest_label,
) -> list[dict[str, Any]]:
    """Token shares, largest first, with everything past ``shown`` as one slice named by ``label(count)``."""
    kept, rest = _split_ranked(shares, shown)
    if not rest:
        return kept
    return [*kept, {
        "model": "", "label": label(len(rest)), "other": True,
        "share_of_tokens": sum(row["share_of_tokens"] for row in rest),
        "tokens": sum(row["tokens"] for row in rest),
    }]


def group_spend(rows: list[dict[str, Any]], shown: int = SHOWN) -> list[dict[str, Any]]:
    """Spend's By model rows, costliest first, with everything past ``shown`` as "N more models".

    The remainder keeps SQL's SUM semantics (its cost is the known sum, None only
    when no member carried one, its unpriced calls stay counted) and takes its
    class from ``cost_class`` over its members, the rule Models uses.
    """
    kept, rest = _split_ranked(rows, shown)
    if not rest:
        return kept
    known = [row["cost_usd"] for row in rest if row["cost_usd"] is not None]
    outcomes = [_SPEND_OUTCOME[row["cost_class"]] for row in rest]
    return [*kept, {
        "model": "", "label": rest_label(len(rest)), "other": True,
        "cost_usd": sum(known) if known else None,
        **{key: sum(int(row.get(key) or 0) for row in rest)
           for key in ("prompt_tokens", "completion_tokens", "total_tokens", "unknown_cost_rows", "api_calls")},
        "cost_class": cost_class(outcomes), "includes_local": includes_local(outcomes),
    }]
