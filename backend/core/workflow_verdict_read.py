"""The verdict's read phase: workflow runs, their groups and settled facts from real history (design §3.1-§3.5).

``read_snapshot(db)`` only reads, so it runs on ``run_read``. ``price_snapshot(db, snapshot)`` prices each run
through Measurement's own chain (``api.measurement_model_cost``); ``materialize_chain`` fills a TEMP table, which
the ``query_only`` read lane refuses, so it runs on ``run_bulk`` as Measurement's chain reads do. Neither writes
the main database; ``core.workflow_verdict_rules.compute`` then runs off the database.

Owners this module calls rather than copies: ``tools.workflow_task_fields.judged_version`` (which versions are
judged), ``core.correction_attribution`` (which run a correction belongs to), ``api.cost_identity`` (model
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

from api.cost_identity import IDENTITY_GROUP, IdentityResolver, identity_columns, identity_join
from api.measurement_model_cost import clear_chain, materialize_chain, query_model_costs, query_recording_gaps
from api.measurement_queries import outcome_ctes
from api.measurement_scope import query_params, scope_cte
from core.correction_attribution import attributed_facts, workflow_run_of
from core.measurement_scope import Grain
from core.workflow_verdict_rules import Definition, FrozenPlan, Snapshot, WorkflowHistory
from core.workflow_verdicts import MIXED, OUTCOME_STATES, TERMINAL_STATES, CorrectionFact, KpiRow, Run, parse_ts
from tools.workflow_owners import thread_workflow_owners
from tools.workflow_task_fields import judged_version

MODEL_SHARE = 0.8  # design §3.3: the run's own fresh tokens on one identity
PLANNED_EVENT, REACHED_EVENT = "measurement.verdict.planned", "measurement.verdict.reached"
_BANDS = {"XS": "XS-S", "S": "XS-S", "M": "M", "L": "L+", "XL": "L+", "XXL": "L+"}
# The KPI formula engine (KPI slice 2) supplies these rows; with no reader every deciding KPI is Not comparable.
KpiReader = Callable[[sqlite3.Connection, Sequence[Run], Sequence[Definition]],
                     Mapping[str, Mapping[str, tuple[KpiRow, ...]]]]


def _ids(values: Iterable[str]) -> str:
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
    """, {"ids": _ids(run_ids)}).fetchall()
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


def _terminal_times(db: sqlite3.Connection, node_ids: Sequence[str]) -> dict[tuple[str, str], str]:
    """The first time each node entered each terminal state; both accepted states count as one."""
    rows = db.execute("""
        SELECT node_id, CASE WHEN new_state='accepted_with_followup' THEN 'accepted' ELSE new_state END AS state,
          MIN(created_at) AS at FROM plan_node_events
        WHERE node_id IN (SELECT value FROM json_each(:ids))
          AND new_state IN ('accepted','accepted_with_followup','failed','closed')
        GROUP BY node_id, 2
    """, {"ids": _ids(node_ids)}).fetchall()
    return {(row["node_id"], row["state"]): row["at"] for row in rows}


def _nodes(db: sqlite3.Connection, plan_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    where = "" if plan_ids is None else "WHERE plan_id IN (SELECT value FROM json_each(:plans))"
    return [dict(row) for row in db.execute(f"""
        SELECT id, plan_id, parent_id, kind, state, assignee, workflow_ref, workflow_version, difficulty, size,
          created_at, updated_at FROM plan_nodes {where}
    """, {} if plan_ids is None else {"plans": _ids(plan_ids)}).fetchall()]


def run_roots(nodes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Nodes carrying a workflow with no ancestor carrying the same workflow (design §3.1)."""
    by_id = {node["id"]: node for node in nodes}
    roots = []
    for node in nodes:
        if not node["workflow_ref"]:
            continue
        parent, seen = by_id.get(node["parent_id"]), set()
        while parent is not None and parent["id"] not in seen and parent["workflow_ref"] != node["workflow_ref"]:
            seen.add(parent["id"])
            parent = by_id.get(parent["parent_id"])
        if parent is None or parent["workflow_ref"] != node["workflow_ref"]:
            roots.append(node)
    return roots


def _definitions(db: sqlite3.Connection) -> tuple[dict[str, list[Definition]], dict[str, str]]:
    """Each workflow's definition history, attributed through its live owner and sidecar path.

    ``measure_definitions`` keeps declaration history, but never a workflow-owner registry. The
    effective owner rule remains ``tools.workflow_owners``: a definition counts only while its
    owner's current workflow skill still names the same ``measures.yaml`` beside its SKILL.md.
    """
    rows = [dict(row) for row in db.execute(
        "SELECT owner_thread_id, kind, name, version, spec_json, file_path, synced_at, removed FROM measure_definitions"
    ).fetchall()]
    workflow_of: dict[tuple[str, str], tuple[str, str]] = {}
    for owner in {str(row["owner_thread_id"]) for row in rows}:
        for workflow, entry in thread_workflow_owners(owner, db).items():
            if entry.owner != owner or entry.named is not None:
                continue
            workflow_of[(owner, str(entry.skill_dir / "measures.yaml"))] = (workflow, owner)
    by_workflow: dict[str, list[Definition]] = defaultdict(list)
    owners: dict[str, str] = {}
    for row in rows:
        association = workflow_of.get((row["owner_thread_id"], str(Path(row["file_path"]))))
        if association is None:
            continue
        workflow, owner = association
        owners[workflow] = owner
        by_workflow[workflow].append(Definition(row["kind"], row["name"], int(row["version"]),
                                                json.loads(row["spec_json"]), row["synced_at"],
                                                bool(row["removed"]), row["file_path"]))
    return by_workflow, owners


def _recorded(db: sqlite3.Connection) -> tuple[dict[str, FrozenPlan], frozenset[str]]:
    plans, reached = {}, set()
    for row in db.execute("""
        SELECT subject_key, event_name, attributes FROM outcome_events
        WHERE event_name IN (?, ?) AND retracted_at IS NULL AND subject_key IS NOT NULL
    """, (PLANNED_EVENT, REACHED_EVENT)).fetchall():
        if row["event_name"] == REACHED_EVENT:
            reached.add(row["subject_key"])
        else:
            plans[row["subject_key"]] = FrozenPlan.decoded(json.loads(row["attributes"])["plan"])
    return plans, frozenset(reached)


def _terminal_at(node: Mapping[str, Any], times: Mapping[tuple[str, str], str]) -> str | None:
    if node["state"] not in TERMINAL_STATES:
        return None
    state = "accepted" if node["state"] in OUTCOME_STATES else node["state"]
    return times.get((node["id"], state)) or node["updated_at"]


def read_snapshot(db: sqlite3.Connection, *, now: datetime | None = None,
                  kpi_reader: KpiReader | None = None) -> Snapshot:
    """Every judged workflow run with its group, settlement and attributed corrections; costs come later."""
    now = now or datetime.now(timezone.utc)
    nodes = _nodes(db)
    parents = {node["id"]: node["parent_id"] for node in nodes}
    roots = run_roots(nodes)
    facts: dict[str, list[CorrectionFact]] = defaultdict(list)
    root_ids = [node["id"] for node in roots]
    for fact in attributed_facts(db):
        run_id = workflow_run_of(fact.node_id, parents, root_ids)
        facts[run_id].append(CorrectionFact(fact.message_id, fact.message_at, fact.counter, fact.is_correction))
    judged = [node for node in roots if judged_version(node["workflow_version"])]
    models = _model_groups(db, [node["id"] for node in judged])
    times = _terminal_times(db, [node["id"] for node in judged])
    definitions, owners = _definitions(db)
    by_workflow: dict[str, list[Run]] = defaultdict(list)
    excluded: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for node in roots:
        if node not in judged:
            reason = "local_edit" if node["workflow_version"] else "unversioned"
            excluded[node["workflow_ref"]][reason] += 1
            continue
        group = (models.get(node["id"], MIXED), node["difficulty"] or "unrated", size_band(node["size"]))
        by_workflow[node["workflow_ref"]].append(Run(
            node["id"], node["workflow_version"], group, node["created_at"], _terminal_at(node, times), node["state"],
            tuple(sorted(facts.get(node["id"], ()), key=lambda f: (parse_ts(f.message_at), f.message_id)))))
    histories = []
    for ref in sorted(set(by_workflow) | set(excluded)):
        runs = by_workflow.get(ref, [])
        if kpi_reader is not None and runs:
            rows = kpi_reader(db, runs, definitions.get(ref, []))
            runs = [replace(run, kpi_rows=rows.get(run.run_id, {})) for run in runs]
        histories.append(WorkflowHistory(ref, owners.get(ref), tuple(runs), tuple(definitions.get(ref, ())),
                                         excluded={k: v for k, v in excluded.get(ref, {}).items()}))
    plans, reached = _recorded(db)
    return Snapshot(now.isoformat(), tuple(histories), plans, reached)


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
    """Each settled run's cost: the API-equivalent price of every accepted task's chain in its subtree.

    Unknown (None), never 0, when any task in the subtree is outside Measurement's accepted set (failed,
    closed, still open, corrected or out of scope), when any call can't be priced, or when a call failed
    to reach the ledger.
    """
    runs = [run for history in snapshot.workflows for run in history.runs if run.terminal_at]
    if not runs:
        return snapshot
    plan_of = {row["id"]: row["plan_id"] for row in db.execute(
        "SELECT id, plan_id FROM plan_nodes WHERE id IN (SELECT value FROM json_each(:ids))",
        {"ids": _ids(run.run_id for run in runs)}).fetchall()}
    trees = _subtrees(_nodes(db, set(plan_of.values())), [run.run_id for run in runs])
    tasks = {node["id"] for tree in trees.values() for node in tree}
    users = [row["user_id"] for row in db.execute("""
        SELECT DISTINCT t.user_id FROM plans p JOIN threads t ON t.id=p.owner_id
        WHERE p.owner_kind='thread' AND p.id IN (SELECT value FROM json_each(:plans))
    """, {"plans": _ids(plan_of.values())}).fetchall()]
    first = min(parse_ts(run.started_at) for run in runs).date()
    grain = history_grain(first, parse_ts(snapshot.now))
    eligible: set[str] = set()
    unknown: set[str] = set()
    dollars: defaultdict[str, float] = defaultdict(float)
    for user_id in users:
        params = {"user_id": user_id, "scope_project": "all", "scope_agent": None, "grain": grain}
        bound = {**query_params(params), "task_ids": _ids(tasks)}
        eligible |= {row["id"] for row in db.execute(
            scope_cte() + outcome_ctes(grain, limited_to_range=False)
            + " SELECT id FROM outcome_rows WHERE id IN (SELECT value FROM json_each(:task_ids))", bound).fetchall()}
        chain = materialize_chain(db, params, tasks)
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
        if not ids or ids - eligible or ids & unknown:
            return None
        return sum(dollars[task_id] for task_id in ids)

    return replace(snapshot, workflows=tuple(
        replace(history, runs=tuple(replace(run, cost=cost(run)) if run.terminal_at else run for run in history.runs))
        for history in snapshot.workflows))
