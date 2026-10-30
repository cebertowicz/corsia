"""The verdict's read phase: workflow runs, their groups and settled facts from real history (design §3.1-§3.5).

``read_snapshot(db)`` only reads, so it runs on ``run_read``. ``price_snapshot(db, snapshot)`` prices each run
through Measurement's own chain (``api.measurement_model_cost``); ``materialize_chain`` fills a TEMP table, which
the ``query_only`` read lane refuses, so it runs on ``run_bulk`` as Measurement's chain reads do. Neither writes
the main database; ``core.workflow_verdict_rules.compute`` then runs off the database.

Owners this module calls rather than copies: ``tools.workflow_task_fields.judged_version`` (which versions are
judged), ``core.correction_attribution`` (which run a correction belongs to), ``core.cost_identity`` (model
identity) and ``api.measurement_model_cost`` with ``api.measurement_queries.outcome_ctes`` (cost).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.cost_identity import IDENTITY_GROUP, IdentityResolver, identity_columns, identity_join
from api.measurement_model_cost import clear_chain, materialize_chain, query_model_costs, query_recording_gaps
from api.measurement_queries import outcome_ctes
from api.measurement_scope import query_params, scope_cte
from core.correction_attribution import attributed_facts, workflow_run_of
from core.measurement_scope import Grain
from infra.measure_registry import workflow_for_file
from core.workflow_verdict_rules import Definition, FrozenPlan, Snapshot, WorkflowHistory
from core.workflow_verdict_types import split_subject, subject_of
from core.workflow_verdicts import MIXED, OUTCOME_STATES, TERMINAL_STATES, CorrectionFact, KpiRow, Run, parse_ts
from tools.workflow_owners import thread_workflow_owners
from tools.workflow_task_fields import judged_version

MODEL_SHARE = 0.8  # design §3.3: the run's own fresh tokens on one identity
PLANNED_EVENT, REACHED_EVENT = "measurement.verdict.planned", "measurement.verdict.reached"
OPEN_BATCH_LIMIT = 100
# Workflow versions in use, latest first, without a reached verdict: the recorded plan is written by the job
# that first evaluates a version, so an open version is one the plan nodes show and no reached event closes.
_OPEN_WORKFLOWS_SQL = """
    SELECT used.ref, used.version FROM (
      SELECT workflow_ref AS ref, workflow_version AS version, MAX(created_at) AS latest FROM plan_nodes
      WHERE workflow_ref IS NOT NULL AND workflow_version IS NOT NULL GROUP BY workflow_ref, workflow_version
    ) used
    WHERE NOT EXISTS (
      SELECT 1 FROM outcome_events reached
      WHERE reached.event_name=:reached AND reached.retracted_at IS NULL
        AND reached.subject_key='workflow:'||used.ref||'@'||used.version)
    ORDER BY used.latest DESC, used.ref, used.version
"""
_BANDS = {"XS": "XS-S", "S": "XS-S", "M": "M", "L": "L+", "XL": "L+", "XXL": "L+"}
# The KPI formula engine (KPI slice 2) supplies these rows; with no reader every deciding KPI is Not comparable.
KpiReader = Callable[[sqlite3.Connection, Sequence[Run], Sequence[Definition]],
                     Mapping[str, Mapping[str, tuple[KpiRow, ...]]]]


def json_ids(values: Iterable[str]) -> str:
    """The distinct ids as the JSON array a ``json_each(:ids)`` filter binds."""
    return json.dumps(sorted(set(values)))


def size_band(size: object) -> str:
    return _BANDS.get(str(size), "unsized")


def model_key(identity_key: str) -> str:
    """One model whatever harness or subscription ran it: the identity without its transport prefix."""
    return identity_key.rpartition("/")[2] or identity_key


def _model_groups(db: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, str]:
    """Each run's model: the identity behind at least 80% of its own node's fresh tokens, else mixed."""
    rows = db.execute(f"""
        SELECT node_id,{IDENTITY_GROUP},SUM(fresh) AS fresh FROM (
          SELECT u.plan_node_id AS node_id,{identity_columns("u")},
            COALESCE(u.prompt_tokens,0)+COALESCE(u.completion_tokens,0)+COALESCE(u.cache_creation_tokens,0) AS fresh
          FROM llm_usage u {identity_join("u")}
          WHERE u.plan_node_id IN (SELECT value FROM json_each(:ids))
        ) GROUP BY node_id,{IDENTITY_GROUP}
    """, {"ids": json_ids(run_ids)}).fetchall()
    resolve = IdentityResolver(db)
    totals: defaultdict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        totals[row["node_id"]][model_key(resolve(dict(row)).key)] += float(row["fresh"] or 0)
    groups = {}
    for node_id, by_model in totals.items():
        model, tokens = max(by_model.items(), key=lambda item: item[1])
        whole = sum(by_model.values())
        groups[node_id] = model if whole > 0 and tokens / whole >= MODEL_SHARE else MIXED
    return groups


def terminal_times(db: sqlite3.Connection, node_ids: Sequence[str]) -> dict[tuple[str, str], str]:
    """First terminal times by state, normalized as UTC instants."""
    rows = db.execute("""
        SELECT node_id, CASE WHEN new_state='accepted_with_followup' THEN 'accepted' ELSE new_state END AS state,
          MIN(datetime(created_at)) AS at FROM plan_node_events
        WHERE node_id IN (SELECT value FROM json_each(:ids))
          AND new_state IN ('accepted','accepted_with_followup','failed','closed')
        GROUP BY node_id, 2
    """, {"ids": json_ids(node_ids)}).fetchall()
    return {(row["node_id"], row["state"]): row["at"] for row in rows}


def _nodes(db: sqlite3.Connection, plan_ids: Iterable[str] | None = None,
           workflow_refs: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Nodes in all plans, selected plans, or plans that contain selected workflows."""
    if plan_ids is not None and workflow_refs is not None:
        raise ValueError("select nodes by plan ids or workflow refs, not both")
    if workflow_refs is not None:
        refs = json_ids(workflow_refs)
        if refs == "[]":
            return []
        query = """
            WITH selected_plans AS (
              SELECT DISTINCT plan_id FROM plan_nodes
              WHERE workflow_ref IN (SELECT value FROM json_each(:workflow_refs))
            )
            SELECT id, plan_id, parent_id, follow_up_of, kind, state, assignee, workflow_ref, workflow_version, difficulty, size,
              created_at, updated_at FROM plan_nodes
            WHERE plan_id IN selected_plans
        """
        params = {"workflow_refs": refs}
    else:
        query = """
            SELECT id, plan_id, parent_id, follow_up_of, kind, state, assignee, workflow_ref, workflow_version, difficulty, size,
              created_at, updated_at FROM plan_nodes
        """
        params = {} if plan_ids is None else {"plans": json_ids(plan_ids)}
        if plan_ids is not None:
            query += " WHERE plan_id IN (SELECT value FROM json_each(:plans))"
    return [dict(row) for row in db.execute(query, params).fetchall()]


def run_roots(nodes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Nodes carrying a workflow with no ancestor carrying the same workflow (design §3.1).

    A send-back's revision and an accepted step's follow-up keep their source's workflow
    and continue that run.
    """
    by_id = {node["id"]: node for node in nodes}
    roots = []
    for node in nodes:
        if not node["workflow_ref"]:
            continue
        source = by_id.get(node["follow_up_of"])
        if source is not None and source["workflow_ref"] == node["workflow_ref"]:
            continue
        parent, seen = by_id.get(node["parent_id"]), set()
        while parent is not None and parent["id"] not in seen and parent["workflow_ref"] != node["workflow_ref"]:
            seen.add(parent["id"])
            parent = by_id.get(parent["parent_id"])
        if parent is None or parent["workflow_ref"] != node["workflow_ref"]:
            roots.append(node)
    return roots


def workflow_definitions(db: sqlite3.Connection) -> tuple[dict[str, list[Definition]], dict[str, str]]:
    """Each workflow's definition history, attributed through its live owner and sidecar path.

    ``measure_definitions`` keeps declaration history, but never a workflow-owner registry. The
    effective owner rule remains ``tools.workflow_owners``: a definition counts only while its
    owner's current workflow skill still names the same ``measures.yaml`` beside its SKILL.md.
    """
    rows = [dict(row) for row in db.execute(
        "SELECT owner_thread_id, kind, name, version, spec_json, file_path, synced_at, removed, version_attr FROM measure_definitions"
    ).fetchall()]
    workflows = {owner: thread_workflow_owners(owner, db) for owner in {str(row['owner_thread_id']) for row in rows}}
    by_workflow: dict[str, list[Definition]] = defaultdict(list)
    owners: dict[str, str] = {}
    for row in rows:
        owner = row['owner_thread_id']
        workflow = workflow_for_file(row['file_path'], owner, workflows[owner])
        if workflow is None:
            continue
        owners[workflow] = owner
        by_workflow[workflow].append(Definition(row["kind"], row["name"], int(row["version"]),
                                                json.loads(row["spec_json"]), row["synced_at"],
                                                bool(row["removed"]), row["file_path"], row['version_attr']))
    return by_workflow, owners


def open_workflow_subjects(db: sqlite3.Connection, *, limit: int = OPEN_BATCH_LIMIT) -> frozenset[str]:
    """The latest ``limit`` judged workflow versions with no reached verdict.

    Latest use first, so a version that never gets a baseline cannot starve newer ones. A version that is
    not judged (unversioned, a local edit) is skipped here, in Python, and the scan stops at ``limit``.
    """
    if not 1 <= limit <= OPEN_BATCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {OPEN_BATCH_LIMIT}")
    found: list[str] = []
    for row in db.execute(_OPEN_WORKFLOWS_SQL, {"reached": REACHED_EVENT}):
        if judged_version(row["version"]):
            found.append(subject_of("workflow", row["ref"], row["version"]))
            if len(found) == limit:
                break
    return frozenset(found)


def recorded_verdicts(db: sqlite3.Connection, subjects: Iterable[str] | None = None) -> tuple[dict[str, FrozenPlan], frozenset[str]]:
    """Recorded plan and reached state, optionally for one bounded subject batch."""
    query = """
        SELECT subject_key, event_name, attributes FROM outcome_events
        WHERE event_name IN (:planned, :reached) AND retracted_at IS NULL AND subject_key IS NOT NULL
    """
    params: dict[str, object] = {"planned": PLANNED_EVENT, "reached": REACHED_EVENT}
    if subjects is not None:
        encoded = json_ids(subjects)
        if encoded == "[]":
            return {}, frozenset()
        query += " AND subject_key IN (SELECT value FROM json_each(:subjects))"
        params["subjects"] = encoded
    plans, reached = {}, set()
    for row in db.execute(query, params).fetchall():
        if row["event_name"] == REACHED_EVENT:
            reached.add(row["subject_key"])
        else:
            plans[row["subject_key"]] = FrozenPlan.decoded(json.loads(row["attributes"])["plan"])
    return plans, frozenset(reached)


def node_terminal_at(node: Mapping[str, Any], times: Mapping[tuple[str, str], str]) -> str | None:
    if node["state"] not in TERMINAL_STATES:
        return None
    state = "accepted" if node["state"] in OUTCOME_STATES else node["state"]
    return times.get((node["id"], state)) or node["updated_at"]


def read_snapshot(db: sqlite3.Connection, *, now: datetime | None = None,
                  kpi_reader: KpiReader | None = None,
                  workflow_refs: Iterable[str] | None = None) -> Snapshot:
    """Every judged workflow run, optionally only for selected workflow references."""
    now = now or datetime.now(timezone.utc)
    selected = None if workflow_refs is None else frozenset(workflow_refs)
    definitions, owners = workflow_definitions(db)
    nodes = _nodes(db, workflow_refs=selected)
    parents = {node["id"]: node["parent_id"] for node in nodes}
    roots = run_roots(nodes)
    if selected is not None:
        roots = [root for root in roots if root["workflow_ref"] in selected]
    facts: dict[str, list[CorrectionFact]] = defaultdict(list)
    root_ids = [node["id"] for node in roots]
    attributed = attributed_facts(db) if selected is None else attributed_facts(db, node_ids=parents)
    for fact in attributed:
        run_id = workflow_run_of(fact.node_id, parents, root_ids)
        facts[run_id].append(CorrectionFact(
            fact.source_kind, fact.source_id, fact.source_at or "", fact.counter, fact.is_correction,
        ))
    judged = [node for node in roots if judged_version(node["workflow_version"])]
    judged_ids = {node["id"] for node in judged}
    models = _model_groups(db, [node["id"] for node in judged])
    times = terminal_times(db, [node["id"] for node in judged])
    by_workflow: dict[str, list[Run]] = defaultdict(list)
    excluded: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for node in roots:
        if node["id"] not in judged_ids:
            reason = "local_edit" if node["workflow_version"] else "unversioned"
            excluded[node["workflow_ref"]][reason] += 1
            continue
        group = (models.get(node["id"], MIXED), node["difficulty"] or "unrated", size_band(node["size"]))
        by_workflow[node["workflow_ref"]].append(Run(
            node["id"], node["workflow_version"], group, node["created_at"], node_terminal_at(node, times), node["state"],
            tuple(sorted(facts.get(node["id"], ()), key=lambda f: (parse_ts(f.source_at), f.source_id)))))
    histories = []
    for ref in sorted(set(by_workflow) | set(excluded)):
        runs = by_workflow.get(ref, [])
        if kpi_reader is not None and runs:
            rows = kpi_reader(db, runs, definitions.get(ref, []))
            runs = [replace(run, kpi_rows=rows.get(run.run_id, {})) for run in runs]
        histories.append(WorkflowHistory(
            ref, owners.get(ref), tuple(runs), tuple(definitions.get(ref, ())),
            version_attr=next((d.version_attr for d in definitions.get(ref, ()) if not d.removed), None),
            excluded={k: v for k, v in excluded.get(ref, {}).items()},
        ))
    plans, reached = recorded_verdicts(db)
    return Snapshot(now.isoformat(), tuple(histories), plans, reached)


def read_open_snapshot(db: sqlite3.Connection, *, now: datetime | None = None,
                       limit: int = OPEN_BATCH_LIMIT, kpi_reader: KpiReader | None = None) -> Snapshot:
    """One bounded open-verdict batch; selected workflows retain only their own full history."""
    subjects = open_workflow_subjects(db, limit=limit)
    refs = {split_subject(subject)[1] for subject in subjects}
    snapshot = read_snapshot(db, now=now, kpi_reader=kpi_reader, workflow_refs=refs)
    plans, reached = recorded_verdicts(db, subjects)
    return replace(snapshot, plans=plans, reached=reached, subjects=subjects)


def history_grain(first: date, now: datetime) -> Grain:
    """Weekly UTC periods from ``first`` through the week in progress, all treated as complete, so every
    accepted task in the history has a period and Measurement's chain walk seeds from it."""
    start = first - timedelta(days=first.weekday())
    today = now.astimezone(timezone.utc).date()
    after = today - timedelta(days=today.weekday()) + timedelta(days=7)
    firsts = [start + timedelta(days=7 * step) for step in range((after - start).days // 7 + 2)]
    text = [f"{day.isoformat()} 00" for day in firsts]
    return Grain(kind="weeks", zone="UTC", current=firsts[-2].isoformat(),
                 periods=tuple(day.isoformat() for day in firsts[:-2]), starts=tuple(text[:-1]),
                 end=text[-2], current_end=text[-1], precision=13)


def _subtrees(nodes: Sequence[Mapping[str, Any]], run_ids: Iterable[str]) -> dict[str, list[Mapping[str, Any]]]:
    """Each run's tasks in its own plan: itself and every descendant, stopping at another run."""
    children: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in nodes:
        if node["parent_id"]:
            children[node["parent_id"]].append(node)
    by_id, wanted, trees = {node["id"]: node for node in nodes}, set(run_ids), {}
    for run_id in wanted:
        stack, tree = [by_id[run_id]], []
        while stack:
            node = stack.pop()
            tree.append(node)
            stack += [child for child in children[node["id"]] if child["id"] not in wanted]
        trees[run_id] = [node for node in tree if node["kind"] == "task" and (node["assignee"] or "") != "user"]
    return trees


def price_snapshot(db: sqlite3.Connection, snapshot: Snapshot) -> Snapshot:
    """Each settled run's cost: the API-equivalent price of every chain in its subtree, error runs included.

    An accepted task's chain already holds its non-accepted descendants' usage (``measurement_chain``), so each
    is counted once, there. A terminal run that is not an accepted outcome (failed, closed, corrected) is priced
    by the same walk seeded from it and read to its own terminal time.

    Unknown (None), never 0, when any call can't be priced, when a call failed to reach the ledger, or when
    the run has no tasks.
    """
    runs = [run for history in snapshot.workflows for run in history.runs if run.terminal_at]
    if not runs:
        return snapshot
    plan_of = {row["id"]: row["plan_id"] for row in db.execute(
        "SELECT id, plan_id FROM plan_nodes WHERE id IN (SELECT value FROM json_each(:ids))",
        {"ids": json_ids(run.run_id for run in runs)}).fetchall()}
    trees = _subtrees(_nodes(db, set(plan_of.values())), [run.run_id for run in runs])
    tasks = {node["id"] for tree in trees.values() for node in tree}
    owner_of = {row["id"]: row["user_id"] for row in db.execute("""
        SELECT p.id, t.user_id FROM plans p JOIN threads t ON t.id=p.owner_id
        WHERE p.owner_kind='thread' AND p.id IN (SELECT value FROM json_each(:plans))
    """, {"plans": json_ids(plan_of.values())}).fetchall()}
    first = min(parse_ts(run.started_at) for run in runs).date()
    grain = history_grain(first, parse_ts(snapshot.now))
    unknown: set[str] = set()
    dollars: defaultdict[str, float] = defaultdict(float)
    for user_id in set(owner_of.values()):
        params = {"user_id": user_id, "scope_project": "all", "scope_agent": None, "grain": grain}
        bound = {**query_params(params), "task_ids": json_ids(tasks)}
        accepted = {row["id"] for row in db.execute(
            scope_cte() + outcome_ctes(grain, limited_to_range=False)
            + " SELECT id FROM outcome_rows WHERE id IN (SELECT value FROM json_each(:task_ids))", bound).fetchall()}
        seeds = {run.run_id for run in runs if owner_of.get(plan_of.get(run.run_id)) == user_id} - accepted
        chain = materialize_chain(db, params, tasks, seeds)
        try:
            for row in query_model_costs(db, params, IdentityResolver(db), task_ids=tasks, chain_rows=chain):
                if row["dollars"] is None:
                    unknown.add(row["task_id"])
                else:
                    dollars[row["task_id"]] += float(row["dollars"])
            unknown |= query_recording_gaps(db, params, task_ids=tasks, chain_rows=chain)
        finally:
            clear_chain(db)

    def cost(run: Run) -> float | None:
        ids = {node["id"] for node in trees.get(run.run_id, ())}
        if not ids or ids & unknown or owner_of.get(plan_of.get(run.run_id)) is None:
            return None
        return sum(dollars[task_id] for task_id in ids)

    return replace(snapshot, workflows=tuple(
        replace(history, runs=tuple(replace(run, cost=cost(run)) if run.terminal_at else run for run in history.runs))
        for history in snapshot.workflows))
