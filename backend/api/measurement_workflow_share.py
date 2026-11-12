"""The ``workflows`` family: how many of the scope's accepted tasks ran under a workflow.

One rule, read two ways. The Workflows section's coverage headline is this family summed over its
window's periods (``api.measurement_workflows._coverage``), and ``under_workflow_pct`` is the same
rows served per period on ``/api/measurement/series``. A correction to what counts as running under
a workflow therefore lands in both figures at once; a second membership rule would split them.

The task set is Measurement's own accepted-task read (``api.measurement_queries.outcome_ctes`` with
the page grain), so the share's denominator is the same population every other panel on the page
counts. The series route has already settled that read into ``measurement_outcome_nodes`` for its
other panels, and the family reads those rows there instead of settling the scope a second time.
Membership is ``core.correction_attribution.workflow_run_of``'s rule over the scope's runs
(``core.workflow_builtin_operands.scoped_runs``): a task belongs to the nearest run at or above it,
so the two sentinels that mean "no run" are the only exclusions. The series asks ``scoped_runs`` for
the nodes above its own periods' tasks only, so a longer plan history does not make it slower.

Nothing here reads a run's version, state, price or verdict: the share is a count of tasks, so it is
served without the priced history the Workflows section reads (``workflow_verdict_read``), which is
what makes that route too slow to read as a series.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from api.measurement_prepared import PREPARED_OUTCOME_NODES
from api.measurement_queries import outcome_ctes
from core.correction_attribution import NOT_IN_WORKFLOW, UNATTRIBUTED, workflow_run_of
from core.measurement_scope import Grain, MeasurementScope, query_params, scope_cte

#: What ``workflow_run_of`` answers for a task with no run above it, and for one with no node to walk.
NO_RUN = (NOT_IN_WORKFLOW, UNATTRIBUTED)


def workflow_share_family(db, params: dict[str, Any], grain: Grain, periods: Iterable[str], *,
                          user_id: str | None = None, scope: MeasurementScope | None = None,
                          parents: Mapping[str, str | None] | None = None,
                          root_ids: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """One row per period that holds any: ``{period, outcomes, under_workflow}``.

    ``periods`` is the caller's own range: the Workflows headline asks for its window only, the
    series for every period it draws plus its averaging lead-in, so neither reads periods it never
    shows. A period with no accepted task carries no row, like every other family.

    Two callers, two reads:

    - The Workflows section passes ``parents`` and ``root_ids``, the scope's runs it already loaded
      (``scoped_runs``). This then makes the accepted-task read and nothing else.
    - The series route passes ``user_id`` and ``scope``, on a connection where
      ``prepare_outcome_periods`` has settled the accepted tasks for ``params`` and ``grain``. This
      reads those settled rows, then only the nodes above the tasks in ``periods``.
    """
    wanted = {"periods": json.dumps(sorted(set(periods)))}
    in_periods = "WHERE period IN (SELECT value FROM json_each(:periods))"
    if parents is None or root_ids is None:
        from core.workflow_builtin_operands import scoped_runs  # imports api.*, which imports this module

        rows = db.execute(f"SELECT node_id AS id,period FROM {PREPARED_OUTCOME_NODES} {in_periods}", wanted).fetchall()
        nodes, roots = scoped_runs(db, user_id, scope, above=(str(row["id"]) for row in rows))
        parents = {str(row["id"]): row["parent_id"] for row in nodes}
        root_ids = frozenset(str(row["id"]) for row in roots)
    else:
        rows = db.execute(scope_cte() + outcome_ctes(grain) + f" SELECT id,period FROM outcome_rows {in_periods}",
                          {**query_params({**params, "grain": grain}), **wanted}).fetchall()
    tallies: dict[str, list[int]] = {}
    for row in rows:
        under = workflow_run_of(str(row["id"]), parents, root_ids) not in NO_RUN
        tally = tallies.setdefault(str(row["period"]), [0, 0])
        tally[0] += 1
        tally[1] += 1 if under else 0
    return [{"period": period, "outcomes": total, "under_workflow": under}
            for period, (total, under) in sorted(tallies.items())]
