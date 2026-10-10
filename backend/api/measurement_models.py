"""Models comparison: one row per model identity, named and priced by Cost's owners.

Identity comes from ``api.cost_identity`` and price from Cost's admission and
classification, so Models, Cost and Spend name and price a model the same way.
Every figure reads the page grain's windows: tasks, rates, failed turns and the
token share cover its latest window (7 days or 4 weeks), and the window before it
is read only for the change.
Each row also carries one cell per size class: the same $ per accepted task
restricted to that class's tasks, its change, and the share right first try.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from api.cost_identity import IdentityResolver, ModelIdentity
from api.measurement_model_cost import materialize_chain, query_model_costs, query_recording_gaps
from api.measurement_model_turns import query_model_turns
from api.measurement_queries import prepare_outcome_periods, query_family
from api.measurement_size import outcome_sizes
from api.model_groups import (
    MIN_TASKS, ModelTally, PeriodTasks, cost_class, floor_label, group_shares, includes_local, rest_label, split_by_floor,
)
from core.measurement_scope import Grain
from core.size_rating import CLASS_MIDPOINT_HOURS
from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

#: Turns a row needs before its failed-turn rate is shown.
MIN_TURNS = 50
#: Token-share slices that name a model; the rest are one "Other (N more models)" slice, as the approved render draws it.
PIE_NAMED = 6


def _cost_reasons(latest: PeriodTasks, previous: PeriodTasks) -> tuple[str, str, float | None]:
    """Why each cost cell has or lacks a figure, decided here so the page never infers it.

    Returns ``(rate_reason, change_reason, cost_change)``. ``rate_reason`` is one
    of ``none | no_tasks | unpriced | local`` and ``change_reason`` one of
    ``none | new | too_few_before | unpriced | local``; ``none`` means the figure
    is served. A change is a percentage only against a previous period with at
    least ``MIN_TASKS`` accepted tasks; a $0 previous rate is a real rate with no
    percentage, so it is ``none`` with no change.
    """
    rate, prior = latest.rate(), previous.rate()
    current = cost_class(latest.outcomes)
    if rate is not None:
        rate_reason = "none"
    elif current is None:
        rate_reason = "no_tasks"
    else:
        rate_reason = "local" if current == "local" else "unpriced"
    if rate_reason == "local":
        return rate_reason, "local", None
    if rate_reason == "no_tasks":
        # Nothing ran this period, so there is nothing to compare and no cause to name.
        return rate_reason, "none", None
    if not previous.tasks:
        return rate_reason, "new", None
    if len(previous.tasks) < MIN_TASKS:
        return rate_reason, "too_few_before", None
    if rate is None or prior is None:
        return rate_reason, "local" if cost_class(previous.outcomes) == "local" else "unpriced", None
    return rate_reason, "none", round((rate - prior) / prior, 10) if prior else None


def _size_cell(size: str, tally: ModelTally, first_try: Mapping[str, int]) -> dict[str, Any] | None:
    """One size class's $ per accepted task, its change and its first-try share; None under the floor.

    The cell counts the class's API-priceable tasks, as the row's rate does, and
    is served only with ``MIN_TASKS`` of them and some priced dollars. Its change
    is a percentage only against a previous period that met the same floor;
    otherwise it is ``new``. First try is the share of the same tasks.
    """
    latest, previous = tally.latest, tally.previous
    tasks = latest.priceable
    rate = latest.rate()
    if len(tasks) < MIN_TASKS or rate is None or latest.dollars <= 0:
        return None
    prior = previous.rate() if len(previous.priceable) >= MIN_TASKS and previous.dollars > 0 else None
    return {
        "size": size,
        "tasks": len(tasks),
        "cost_per_task": round(rate, 10),
        "cost_change": round((rate - prior) / prior, 10) if prior else None,
        "change_reason": "none" if prior else "new",
        "first_try": sum(first_try.get(task, 0) for task in tasks) / len(tasks),
        "partial": cost_class(latest.outcomes) == "no_pricing",
        "recording_gap": bool(latest.gaps & tasks),
    }


def _size_cells(sizes: Mapping[str, ModelTally], first_try: Mapping[str, int]) -> list[dict[str, Any]]:
    cells = (_size_cell(size, sizes[size], first_try) for size in CLASS_MIDPOINT_HOURS if size in sizes)
    return [cell for cell in cells if cell is not None]


def _row(
    model: str, label: str, tally: ModelTally, not_offered_now: bool,
    sizes: Mapping[str, ModelTally], first_try: Mapping[str, int],
) -> dict[str, Any]:
    latest, previous = tally.latest, tally.previous
    rate = latest.rate()
    rate_reason, change_reason, change = _cost_reasons(latest, previous)
    return {
        "model": model,
        "label": label,
        "tasks": len(latest.tasks),
        "failed_turns_per_100": 100 * tally.failed / tally.turns if tally.turns >= MIN_TURNS else None,
        "recorded_cost_per_task": round(rate, 10) if rate is not None else None,
        # Priced dollars of the latest window, None (never 0) when nothing was priced.
        "spend_usd": round(latest.dollars, 10) if rate is not None else None,
        "cost_class": cost_class(latest.outcomes),
        "includes_local": includes_local(latest.outcomes),
        # A task whose call failed to reach the ledger leaves the figure possibly low.
        "recording_gap": rate is not None and bool(latest.gaps & latest.priceable),
        "rate_reason": rate_reason,
        "cost_change": change,
        "change_reason": change_reason,
        "previous_tasks": len(previous.tasks),
        "not_offered_now": not_offered_now,
        "sizes": _size_cells(sizes, first_try),
    }


def build_models(
    token_rows: list[dict[str, Any]], turn_rows: list[dict[str, Any]], error_rows: list[dict[str, Any]],
    cost_rows: list[dict[str, Any]], *, resolve: Callable[[Mapping[str, Any]], ModelIdentity],
    latest: Collection[str], previous: Collection[str], offered: set[str], gaps: Iterable[str] = (),
    outcomes: Mapping[str, tuple[str | None, int]] | None = None,
) -> dict[str, Any]:
    """Collapse resolved rows into Models rows, token shares and their disclosures.

    ``cost_rows`` are ``query_model_costs`` output, already carrying their
    identity; ``gaps`` are the task ids with a call that failed to reach the
    ledger; ``outcomes`` maps a task id to its size class (None when unsized)
    and first-try mark, from ``api.measurement_size.outcome_sizes``. Grouping
    is owned by ``api.model_groups``: rows under ``MIN_TASKS``
    accepted tasks share one floor row (``model`` empty, ``other`` true) rather
    than being dropped, and token shares past the top six share "Other (N more models)".
    """
    gap_ids = set(gaps)
    outcomes = outcomes or {}
    first_try = {task: mark for task, (_, mark) in outcomes.items()}
    identities: dict[str, ModelIdentity] = {}
    tallies: dict[str, ModelTally] = defaultdict(ModelTally)
    sizes: dict[str, dict[str, ModelTally]] = defaultdict(lambda: defaultdict(ModelTally))
    for row in cost_rows:
        identity: ModelIdentity = row["identity"]
        identities[identity.key] = identity
        key = str(row["period"])
        period = "latest" if key in latest else "previous" if key in previous else None
        if period is None:
            continue
        task_id = str(row["task_id"])
        call = (task_id, str(row["outcome"]), row["dollars"], task_id in gap_ids)
        getattr(tallies[identity.key], period).add_call(*call)
        size = outcomes.get(task_id, (None, 0))[0]
        if size is not None:
            getattr(sizes[identity.key][size], period).add_call(*call)
    for row in turn_rows:
        identity = resolve(row)
        identities[identity.key] = identity
        tallies[identity.key].turns += int(row["turns"])
    unmatched = 0
    for row in error_rows:
        if is_provider_usage_or_quota_limit(row.get("error")):
            continue
        identity = resolve(row)
        identities[identity.key] = identity
        tallies[identity.key].failed += 1
        unmatched += not identity.resolved
    tokens: dict[str, int] = defaultdict(int)
    for row in token_rows:
        identity = resolve(row)
        identities[identity.key] = identity
        tokens[identity.key] += int(row["tokens"] or 0)

    for key in tokens:
        # A model seen only in token share still belongs in the table or its floor row.
        tallies.setdefault(key, ModelTally())
    kept, floor = split_by_floor(tallies)
    rows = [_row(key, identities[key].label, tallies[key], key not in offered, sizes[key], first_try) for key in kept]
    rows.sort(key=lambda row: (-row["tasks"], row["label"], row["model"]))
    if floor is not None:
        # The floor row's cells pool its members' tasks, as its own rate does.
        floor_sizes: dict[str, ModelTally] = defaultdict(ModelTally)
        for key in tallies.keys() - set(kept):
            for size, tally in sizes[key].items():
                floor_sizes[size].merge(tally)
        rows.append({**_row("", floor_label(), floor, False, floor_sizes, first_try), "other": True})

    total = sum(tokens.values())
    return {
        "rows": rows,
        "min_tasks": MIN_TASKS,
        "token_shares": group_shares([
            {"model": key, "label": identities[key].label, "share_of_tokens": amount / total, "tokens": amount}
            for key, amount in sorted(tokens.items(), key=lambda item: (-item[1], item[0])) if total
        ], shown=PIE_NAMED, label=lambda count: f"Other ({rest_label(count)})"),
        "unmatched_failed_turns": unmatched,
        "unresolved_token_share": (
            sum(amount for key, amount in tokens.items() if not identities[key].resolved) / total if total else 0.0
        ),
    }


def two_windows(grain: Grain) -> Grain:
    """The same grain narrowed to the periods of its latest and previous windows.

    The windows are the grain's own and do not move; only the periods nothing here compares are
    dropped, so a task's accepted period and its chain are read for those two windows alone.
    """
    keep = len(grain.latest.periods) + len(grain.previous.periods)
    return replace(grain, periods=grain.periods[-keep:], starts=grain.starts[-(keep + 1):])


def _latest_window_params(params: dict[str, Any]) -> dict[str, Any]:
    """Whole-date usage bounds around the grain's latest window; the family's period bucket makes it exact."""
    grain: Grain = params["grain"]
    start, end = grain.instants(grain.latest)
    return {**params, "from_ts": start[:10], "to_ts": (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()}


def models_payload(
    db, scope_params: dict[str, Any], grain: Grain, catalog_entries: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Read and build Models for one validated scope and the page grain.

    The grain's latest window is read for tasks, prices, turns and token share, and the
    window before it only for the change: one comparison rule, resolved once. The tasks'
    chains are walked once, for both their prices and their recording gaps.
    """
    grain = two_windows(grain)
    params = {**scope_params, "grain": grain}
    prepare_outcome_periods(db, params)
    resolve = IdentityResolver(db)
    turn_rows, error_rows = query_model_turns(db, params)
    chain = materialize_chain(db, params)
    try:
        cost_rows = query_model_costs(db, params, resolve, chain_rows=chain)
        gaps = query_recording_gaps(db, params, chain_rows=chain)
    finally:
        db.execute(f"DROP TABLE IF EXISTS {chain}")  # the pooled connection outlives the request
    return build_models(
        query_family(db, "models", _latest_window_params(params)),
        turn_rows, error_rows, cost_rows, resolve=resolve,
        latest=grain.latest.periods, previous=grain.previous.periods,
        offered=resolve.offered_now(catalog_entries), gaps=gaps,
        outcomes=outcome_sizes(db, params),
    )
