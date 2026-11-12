"""The ``workflows`` family: how many of the scope's accepted tasks ran under a workflow.

One membership rule, ``under_workflow``, counted by two readers: the Workflows section's coverage
headline (``api.measurement_workflows._coverage``) over its window, and ``under_workflow_pct`` per
period on ``/api/measurement/series``. A correction to what counts as running under a workflow
therefore lands in both figures at once; a second membership rule would split them.

Both count Measurement's own accepted tasks (``api.measurement_queries.outcome_ctes`` with the page
grain), so the share's denominator is the population every other panel on the page counts. Each
reader keeps the read it already has: the headline loads the whole scope's runs for the section it
lists, and the series reads the tasks its request settled and only the nodes reachable from them.

Nothing here reads a run's version, state, price or verdict: the share is a count of tasks, so it is
served without the priced history the Workflows section reads (``workflow_verdict_read``), which is
what makes that route too slow to read as a series.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from api.measurement_prepared import PREPARED_OUTCOME_NODES
from core.correction_attribution import NOT_IN_WORKFLOW, UNATTRIBUTED, workflow_run_of
from core.measurement_scope import MeasurementScope


def under_workflow(task: str, parents: Mapping[str, str | None], root_ids: frozenset[str]) -> bool:
    """Whether a task ran under a workflow: ``workflow_run_of`` finds a run at or above it.

    The two sentinels that mean "no run" (no run above it, no node to walk) are the only exclusions.
    """
    return workflow_run_of(task, parents, root_ids) not in (NOT_IN_WORKFLOW, UNATTRIBUTED)


def workflow_share_family(db, user_id: str, scope: MeasurementScope,
                          periods: Iterable[str]) -> list[dict[str, Any]]:
    """The series' rows, one per period that holds any task: ``{period, outcomes, under_workflow}``.

    Like the other families ``api.measurement._series_read`` serves, this reads the tables that
    request prepared on ``db`` (``prepare_outcome_periods``): the accepted tasks, settled once for
    every panel. ``periods`` is every period the series draws plus its averaging lead-in. A period
    with no accepted task carries no row, like every other family.
    """
    from core.workflow_builtin_operands import scoped_runs  # imports api.*, which imports this module

    tasks = db.execute(
        f"SELECT node_id AS id,period FROM {PREPARED_OUTCOME_NODES}"
        " WHERE period IN (SELECT value FROM json_each(:periods))",
        {"periods": json.dumps(sorted(set(periods)))}).fetchall()
    nodes, roots = scoped_runs(db, user_id, scope, above=(str(task["id"]) for task in tasks))
    parents = {str(row["id"]): row["parent_id"] for row in nodes}
    root_ids = frozenset(str(row["id"]) for row in roots)
    tallies: dict[str, list[int]] = {}
    for task in tasks:
        tally = tallies.setdefault(str(task["period"]), [0, 0])
        tally[0] += 1
        tally[1] += under_workflow(str(task["id"]), parents, root_ids)
    return [{"period": period, "outcomes": total, "under_workflow": under}
            for period, (total, under) in sorted(tallies.items())]
