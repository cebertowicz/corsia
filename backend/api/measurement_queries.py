"""Canonical, parameterized Measurement aggregate query builders.

Every aggregate reads the same composite scope and the same settlement-period
outcome aggregate. ``prepare_outcome_periods`` materializes both once per request
into temporary tables that the family reads then join.
"""

from __future__ import annotations

from typing import Any

from api.cost_identity import IDENTITY_GROUP, identity_columns, identity_join
from api.measurement_prepared import (  # noqa: F401 -- the prepared-table names are re-exported
    DDL as _DDL, PREPARED_OUTCOME_NODES, PREPARED_OUTCOMES, PREPARED_ROOT_DIFFICULTIES,
    PREPARED_ROOT_OUTCOMES, PREPARED_ROOTS, PREPARED_SCOPE, create_tables, shared_prepare,
)
from api.measurement_presence import active_family_sql, presence_family_sql
from api.presence_rollup import rollup_binds
from api.measurement_decision_sql import ANY_OTHER, ANY_TOOK, IS_ANY
from api.measurement_scope import query_params, scope_cte
from api.measurement_token_sql import ROLLUP_TOKEN_SUM_SQL, CACHED_TOKEN_SQL
from core.measurement_scope import Grain
from api.usage_rollup import usage_params, usage_source



def range_filter(column: str) -> str:
    """Return one sargable timestamp range."""
    return f"{column} >= :from_ts AND {column} < :to_ts"


_SPAWNED_ROOTS = """, spawned_roots AS (
          SELECT t.id FROM threads t WHERE t.spawned_by IS NOT NULL
          UNION SELECT d.spawned_thread_id FROM plan_nodes d WHERE d.spawned_thread_id IS NOT NULL
        )"""


def _rejection_periods(grain: Grain | None) -> str:
    return f""", rejection_periods AS (
          SELECT {grain.bucket('e.created_at') if grain else 'NULL'} AS period, COUNT(*) AS rejections
          FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
          JOIN plans rp ON rp.id=n.plan_id
          WHERE e.event='review_rejected' AND n.plan_id IN (SELECT id FROM top_plans)
            AND rp.owner_id NOT IN (SELECT id FROM spawned_roots)
            AND {range_filter("e.created_at")} GROUP BY period
        )"""


def rejection_ctes(grain: Grain | None = None) -> str:
    """Return ``spawned_roots`` and ``rejection_periods`` for the scope's ``top_plans``.

    ``outcome_ctes`` carries the same two CTEs; a reader that wants only the send-backs appends these and
    never names the settlement.
    """
    return _SPAWNED_ROOTS + _rejection_periods(grain)


def outcome_ctes(grain: Grain | None = None, *, plans: str = "top_plans", limited_to_range: bool = True) -> str:
    """Return the canonical accepted-task CTEs for one eligible-plan set.

    With a ``grain`` each task is tagged with its period as ``period`` (NULL outside the
    completed range) and, when ``limited_to_range``, only tasks accepted in that range are
    kept; with no grain every accepted task is kept and ``period`` is NULL.

    ``first_try`` is 1 only when the settled node *and* the node it revises
    (``follow_up_of``) are outside the reworked set: sent back
    (``review_rejected``), released bad (``failed``/``requirements_changed``),
    or failed. An accepted revision of a sent-back task is therefore not a
    first try. Each send-back marks the node it rejects, so the immediate
    predecessor already carries the mark at every depth and one hop is enough.
    """
    return f"""
        , settle AS (
          SELECT node_id, MIN(created_at) AS settled_at FROM plan_node_events
          WHERE new_state IN ('accepted','accepted_with_followup') GROUP BY node_id
          {f"HAVING {grain.bucket('MIN(created_at)')} IS NOT NULL" if grain and limited_to_range else ""}
        ), corrections AS (
          SELECT node_id FROM plan_node_events WHERE event='correction_created' GROUP BY node_id
        ), rejected AS (
          SELECT node_id FROM plan_node_events WHERE event='review_rejected' GROUP BY node_id
        ), released_bad AS (
          SELECT node_id FROM plan_node_attempts
          WHERE release_reason IN ('failed','requirements_changed') GROUP BY node_id
        ), reworked AS (
          SELECT node_id FROM rejected
          UNION SELECT node_id FROM released_bad
          UNION SELECT id FROM plan_nodes WHERE state='failed'
        ){_SPAWNED_ROOTS}, outcome_rows AS (
          SELECT n.id,n.commitment_id,p.owner_id AS root_id,n.difficulty,s.settled_at,
            {grain.bucket('s.settled_at') if grain else 'NULL'} AS period,
            CASE WHEN n.id IN (SELECT node_id FROM reworked)
              OR (n.follow_up_of IS NOT NULL AND n.follow_up_of IN (SELECT node_id FROM reworked)
              ) THEN 0 ELSE 1 END AS first_try
          FROM plan_nodes n JOIN settle s ON s.node_id=n.id
          LEFT JOIN plans p ON p.id=n.plan_id
          WHERE n.kind='task' AND n.state IN ('accepted','accepted_with_followup')
            AND n.plan_id IN (SELECT id FROM {plans})
            AND COALESCE(n.assignee,'')<>'user' AND n.id NOT IN (SELECT node_id FROM corrections)
            AND p.owner_id NOT IN (SELECT id FROM spawned_roots)
        ), outcome_periods AS (
          SELECT period, COUNT(*) AS outcomes, SUM(first_try) AS first_try FROM outcome_rows GROUP BY period
        ){_rejection_periods(grain)}
    """


def prepare_scope_threads(db, params: dict[str, Any]) -> None:
    """Materialize the scope's threads alone, for a read of the usage ledger that needs no outcome periods."""
    db.execute(_DDL[PREPARED_SCOPE])
    db.execute("DELETE FROM measurement_scope_threads")
    db.execute(scope_cte() + "INSERT INTO measurement_scope_threads(id,root_id) SELECT id,root_id FROM scope_threads", query_params(params))


def _materialize_outcome_periods(db, params: dict[str, Any], *, roots: bool) -> None:
    """Materialize the shared scope and outcome aggregate for one request connection.

    The accepted outcomes are settled **once**, into ``measurement_outcome_nodes``
    (each with the agent that owns its plan). Every other grain is a ``GROUP BY`` over
    those rows: the scope totals, and with ``roots`` the per-agent and per-difficulty
    grains the Agents surface reads. ``api.measurement_size`` reads the same rows to
    size each outcome, so no reader of this connection settles the scope again, and the
    totals agree because they are one population, not several runs of one statement.
    Only the rejection counts come from their own statement: they are not outcomes.
    """
    create_tables(db, roots)
    prepare_scope_threads(db, params)
    db.execute("DELETE FROM measurement_outcome_periods")
    if roots:
        db.execute("DELETE FROM measurement_scope_roots")
        db.execute(scope_cte() + "INSERT INTO measurement_scope_roots(id) SELECT id FROM scope_roots", query_params(params))
    db.execute("DELETE FROM measurement_outcome_nodes")
    db.execute(scope_cte() + outcome_ctes(params["grain"]) + """
        INSERT INTO measurement_outcome_nodes(node_id,root_id,period,first_try,difficulty)
        SELECT id,root_id,period,first_try,difficulty FROM outcome_rows
    """, query_params(params))
    if roots:
        db.execute("DELETE FROM measurement_root_outcome_periods")
        db.execute("""
            INSERT INTO measurement_root_outcome_periods(root_id,period,outcomes,first_try)
            SELECT root_id,period,COUNT(*),SUM(first_try) FROM measurement_outcome_nodes GROUP BY root_id,period
        """)
        db.execute("DELETE FROM measurement_root_difficulty_periods")
        db.execute("""
            INSERT INTO measurement_root_difficulty_periods(root_id,period,difficulty,count)
            SELECT root_id,period,COALESCE(difficulty,'unclassified'),COUNT(*)
            FROM measurement_outcome_nodes GROUP BY root_id,period,COALESCE(difficulty,'unclassified')
        """)
    db.execute(scope_cte() + rejection_ctes(params["grain"]) + """
        INSERT INTO measurement_outcome_periods(period,outcomes,first_try,rejections)
        SELECT o.period,o.outcomes,o.first_try,COALESCE(r.rejections,0)
        FROM (SELECT period,COUNT(*) AS outcomes,SUM(first_try) AS first_try
              FROM measurement_outcome_nodes GROUP BY period) o LEFT JOIN rejection_periods r USING(period)
    """, query_params(params))


def prepare_outcome_periods(db, params: dict[str, Any], *, roots: bool = False) -> None:
    """Materialize the shared scope and outcome aggregate for one request connection.

    Overlapping same-key callers share one computation (``api.measurement_prepared``);
    every caller ends with the tables ``_materialize_outcome_periods`` would have built.
    """
    shared_prepare(db, params, roots, lambda: _materialize_outcome_periods(db, params, roots=roots))



def _token_family_sql(grain: Grain, scope_threads: str, outcomes: str) -> str:
    """Return the scope's weekly token totals: fresh, cache re-reads and calls with no count.

    One row per period with usage, summed across every harness: the total is the figure,
    and a split by harness would leave the reader to add the lines up to learn it. The
    period still in progress is included; the readers that ignore it find no row for it.
    """
    return f"""
      WITH token_periods AS (
        SELECT {grain.bucket('u.hour', partial=True)} AS period,
          {ROLLUP_TOKEN_SUM_SQL} AS tokens,SUM(u.fresh_unknown_records) AS unknown_records,
          SUM({CACHED_TOKEN_SQL}) AS cached_tokens,SUM(u.records) AS usage_rows
        FROM {usage_source(scope_threads, grain)} u GROUP BY period
      ) SELECT t.period,t.tokens,t.unknown_records,t.cached_tokens,t.usage_rows,COALESCE(o.outcomes,0) AS outcomes
        FROM token_periods t LEFT JOIN {outcomes} o USING(period) ORDER BY t.period
    """


def decision_family_sql(grain: Grain, scope_threads: str, outcomes: str) -> str:
    return f"""
      WITH scoped_decisions AS (
        SELECT ai.metadata FROM artifact_identities ai
        WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
          AND json_extract(ai.metadata,'$.plan_decision.thread_id') IN (SELECT id FROM {scope_threads})
      ), ask_periods AS (
        SELECT {grain.bucket("json_extract(metadata,'$.plan_decision.raised_at')")} AS period,
          COUNT(*) AS raised FROM scoped_decisions WHERE json_extract(metadata,'$.plan_decision.raised_at') IS NOT NULL
            AND {range_filter("json_extract(metadata,'$.plan_decision.raised_at')")} GROUP BY period
      ), hold_periods AS (
        SELECT {grain.bucket('h.created_at')} AS period,COUNT(*) AS user_holds
        FROM plan_node_holds h JOIN plan_nodes n ON n.id=h.node_id JOIN plans p ON p.id=n.plan_id
        WHERE h.owner_kind='user' AND p.owner_id IN (SELECT id FROM {scope_threads})
          AND {range_filter("h.created_at")} GROUP BY period
      ), answered_periods AS (
        SELECT {grain.bucket("json_extract(metadata,'$.plan_decision.answered_at')")} AS period,
          COUNT(*) AS answered,
          SUM(CASE WHEN {IS_ANY} THEN {ANY_TOOK}
                   ELSE COALESCE(json_extract(metadata,'$.plan_decision.answer.option_id')=json_extract(metadata,'$.plan_decision.recommendation'),0) END) AS recommendation,
          SUM(CASE WHEN {IS_ANY} THEN {ANY_OTHER}
                   ELSE COALESCE(json_extract(metadata,'$.plan_decision.answer.option_id')='other',0) END) AS other,
          SUM(CASE WHEN {IS_ANY} THEN 1 - {ANY_TOOK} - {ANY_OTHER}
                   WHEN json_extract(metadata,'$.plan_decision.answer.option_id') IS NOT NULL
                    AND json_extract(metadata,'$.plan_decision.recommendation') IS NOT NULL
                    AND json_extract(metadata,'$.plan_decision.answer.option_id') != 'other'
                    AND json_extract(metadata,'$.plan_decision.recommendation') != 'other'
                    AND json_extract(metadata,'$.plan_decision.answer.option_id') != json_extract(metadata,'$.plan_decision.recommendation')
                   THEN 1 ELSE 0 END) AS neither
        FROM scoped_decisions WHERE json_extract(metadata,'$.plan_decision.state')='answered'
          AND json_extract(metadata,'$.plan_decision.answered_at') IS NOT NULL
          AND {range_filter("json_extract(metadata,'$.plan_decision.answered_at')")} GROUP BY period
      ) SELECT o.period,o.outcomes,COALESCE(d.raised,0) AS raised,COALESCE(h.user_holds,0) AS user_holds,
          COALESCE(a.answered,0) AS answered,COALESCE(a.recommendation,0) AS recommendation,
          COALESCE(a.other,0) AS other,COALESCE(a.neither,0) AS neither
        FROM {outcomes} o LEFT JOIN ask_periods d USING(period) LEFT JOIN hold_periods h USING(period)
          LEFT JOIN answered_periods a USING(period) ORDER BY o.period
    """


def root_token_family_sql(grain: Grain, scope_threads: str, root_outcomes: str) -> str:
    """Return the token family at the grain of the agent that owns each thread.

    Same numerator the scope family uses, grouped by owning agent, plus the
    usage-row count that says whether a period was measured at all. An
    agent-period with no usage rows is absent rather than zero.
    """
    return f"""
      WITH token_roots AS (
        SELECT s.root_id AS root_id,{grain.bucket('u.hour')} AS period,
          {ROLLUP_TOKEN_SUM_SQL} AS tokens,SUM(u.records) AS usage_rows
        FROM {usage_source(scope_threads, grain)} u JOIN {scope_threads} s ON s.id=u.thread_id
        GROUP BY s.root_id,period
      ) SELECT t.root_id,t.period,t.tokens,t.usage_rows,COALESCE(o.outcomes,0) AS outcomes
        FROM token_roots t LEFT JOIN {root_outcomes} o ON o.root_id=t.root_id AND o.period=t.period
        ORDER BY t.root_id,t.period
    """


def root_decision_family_sql(grain: Grain, scope_threads: str, root_outcomes: str) -> str:
    """Return the decision family attributed to the agent that owns each thread."""
    return f"""
      WITH scoped_decisions AS (
        SELECT s.root_id AS root_id,ai.metadata FROM artifact_identities ai
        JOIN {scope_threads} s ON s.id=json_extract(ai.metadata,'$.plan_decision.thread_id')
        WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
      ), ask_roots AS (
        SELECT root_id,{grain.bucket("json_extract(metadata,'$.plan_decision.raised_at')")} AS period,
          COUNT(*) AS raised FROM scoped_decisions WHERE json_extract(metadata,'$.plan_decision.raised_at') IS NOT NULL
          AND {range_filter("json_extract(metadata,'$.plan_decision.raised_at')")} GROUP BY root_id,period
      ), hold_roots AS (
        SELECT s.root_id AS root_id,{grain.bucket('h.created_at')} AS period,COUNT(*) AS user_holds
        FROM plan_node_holds h JOIN plan_nodes n ON n.id=h.node_id JOIN plans p ON p.id=n.plan_id
        JOIN {scope_threads} s ON s.id=p.owner_id
        WHERE h.owner_kind='user' AND {range_filter("h.created_at")} GROUP BY s.root_id,period
      ) SELECT o.root_id,o.period,o.outcomes,o.first_try,COALESCE(d.raised,0) AS raised,
          COALESCE(h.user_holds,0) AS user_holds
        FROM {root_outcomes} o LEFT JOIN ask_roots d ON d.root_id=o.root_id AND d.period=o.period
          LEFT JOIN hold_roots h ON h.root_id=o.root_id AND h.period=o.period
        ORDER BY o.root_id,o.period
    """


def model_family_sql(grain: Grain, scope_threads: str) -> str:
    """Return the latest window's tokens per literal model and its manifest identity.

    It is the share numerator: fresh tokens plus cache re-reads, the total the Tokens figure
    sums (``TOKEN_FIGURE_SQL``). ``api.cost_identity`` turns each shape into one model identity. Accepted-task
    cost is deliberately not added: it has a different clock and a node-bound join in ``measurement_model_cost``.
    """
    return f"""
      SELECT {identity_columns("u")},{ROLLUP_TOKEN_SUM_SQL}+SUM({CACHED_TOKEN_SQL}) AS tokens
      FROM {usage_source(scope_threads, grain)} u {identity_join("u")}
      WHERE {grain.bucket('u.hour')} BETWEEN '{grain.latest.first}' AND '{grain.latest.last}'
      GROUP BY {IDENTITY_GROUP} ORDER BY tokens DESC,model
    """


def _in_range(rows) -> list[dict[str, Any]]:
    """The rows whose period lies in the grain's range: a timestamp outside it has no period."""
    return [item for item in map(dict, rows) if item.get("period", "") is not None]


def query_family(db, family: str, params: dict[str, Any], *, grouping: str = "scope") -> list[dict[str, Any]]:
    """Read one aggregate family from the request's prepared tables.

    ``grouping="root"`` reads the same formulas at the grain of the agent that
    owns each thread, for the families the Agents table serves. It is a second
    grouping of one query owner, not a second measurement: a family with no root
    grain is refused rather than answered with scope-wide rows.
    """
    bound = usage_params(db, params)
    grain = bound["grain"]
    scope_threads, outcomes = PREPARED_SCOPE, PREPARED_OUTCOMES
    if grouping == "root":
        root_outcomes = PREPARED_ROOT_OUTCOMES
        if family == "outcomes":
            sql = f"SELECT root_id,period,outcomes,first_try FROM {root_outcomes} ORDER BY root_id,period"
        elif family == "tokens":
            sql = root_token_family_sql(grain, scope_threads, root_outcomes)
        elif family == "decisions":
            sql = root_decision_family_sql(grain, scope_threads, root_outcomes)
        else:
            raise ValueError(f"{family} has no root grain")
        return _in_range(db.execute(sql, bound).fetchall())
    if family == "outcomes":
        sql = f"SELECT period,outcomes,first_try,rejections FROM {outcomes} ORDER BY period"
    elif family in ("presence", "active"):
        bound.update(rollup_binds(db, bound))  # no binds, no rolled time: the spans alone answer
        rolled = "focus_rollup_through" in bound
        if family == "presence":
            sql = presence_family_sql(grain, scope_threads, rolled=rolled)
        else:
            sql = active_family_sql(grain, scope_threads, outcomes, rolled=rolled)
    elif family == "decisions":
        sql = decision_family_sql(grain, scope_threads, outcomes)
    elif family == "tokens":
        sql = _token_family_sql(grain, scope_threads, outcomes)
    elif family == "models":
        sql = model_family_sql(grain, scope_threads)
    else:
        return []
    return _in_range(db.execute(sql, bound).fetchall())
