"""Plans, plan nodes and attempts for the fixture, with the census skew.

One project scope plan holds the production maximum of nodes; every other plan
follows the nodes-per-plan quantiles. Thread plans belong to their thread and
hang under the project scope plan, as they do in a real workspace.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import datetime

from .sqlite_pin import sqlite3
from . import fixture_spec as spec
from .fixture_core import AGENT_THREADS, PROJECT_COUNT
from .fixture_rows import Pools, timestamp

PLAN_COUNT = round(spec.PRODUCTION_ROWS["plans"] * spec.SCALE)
PLANS_WITH_NODES = round(2_612 * spec.SCALE)
NODE_STATES = ("pending", "active", "review_requested", "accepted", "accepted", "accepted",
               "accepted_with_followup", "failed", "closed")


def seed_plans(db: sqlite3.Connection, rng: random.Random, pools: Pools) -> list[str]:
    """Insert the workspace scope, project scopes and thread plans; return all plan ids.

    The first project scope (project-00) is the conductor scope.
    """
    stamp = timestamp(rng)
    rows = [("plan-workspace", "workspace_scope", "workspace", None, stamp, stamp)]
    for index in range(PROJECT_COUNT):
        rows.append((f"plan-project-{index:02d}", "project_scope", f"project-{index:02d}",
                     "plan-workspace", stamp, stamp))
    for index in range(PLAN_COUNT - len(rows)):
        thread = index  # one plan per thread, agents first
        parent = f"plan-project-{0 if thread < AGENT_THREADS else rng.randrange(PROJECT_COUNT):02d}"
        rows.append((f"plan-thread-{index:05d}", "thread", f"thread-{index:05d}", parent, stamp, stamp))
    db.executemany("INSERT INTO plans (id,owner_kind,owner_id,parent_plan_id,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?)", rows)
    ids = [r[0] for r in rows]
    pools.add("plan", ids)
    return ids


def _node_counts(rng: random.Random) -> dict[str, int]:
    """Nodes per plan: the conductor scope at the maximum, the rest on the quantiles."""
    quantile = spec.quantile_rows(spec.NODE_KNOTS, PLANS_WITH_NODES - 1)
    rng.shuffle(quantile)
    owners = [f"plan-project-{i:02d}" for i in range(1, PROJECT_COUNT)]
    owners += [f"plan-thread-{i:05d}" for i in range(PLAN_COUNT - PROJECT_COUNT - 1)]
    counts = {"plan-project-00": spec.MAX_PLAN_NODES}
    counts.update(zip(owners, quantile))
    return counts


def _node_rows(rng: random.Random, counts: dict[str, int]) -> Iterator[tuple]:
    for plan_id, count in counts.items():
        owner = f"thread-{int(plan_id[-5:]):05d}" if plan_id.startswith("plan-thread-") else (
            f"thread-{int(plan_id[-2:]) % AGENT_THREADS:05d}")
        phase = None
        for k in range(count):
            node_id = f"node-{plan_id[5:]}-{k:04d}"
            kind = "phase" if plan_id == "plan-project-00" and k % 25 == 0 else "task"
            if kind == "phase":
                phase = node_id
            stamp = timestamp(rng)
            yield (node_id, plan_id, phase if kind == "task" and plan_id == "plan-project-00" else None,
                   k, kind, f"Fixture task {k}", "Fixture acceptance criteria.",
                   NODE_STATES[rng.randrange(len(NODE_STATES))], owner, node_id, stamp, stamp)


def seed_nodes(db: sqlite3.Connection, rng: random.Random, pools: Pools) -> int:
    counts = _node_counts(rng)
    cursor = db.executemany(
        "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,"
        "state,owner_thread_id,commitment_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        _node_rows(rng, counts))
    nodes = [r[0] for r in db.execute("SELECT id FROM plan_nodes ORDER BY rowid")]
    pools.add("node", nodes)
    return cursor.rowcount


def seed_attempts(db: sqlite3.Connection, rng: random.Random, pools: Pools) -> int:
    """Attempts on non-pending nodes, ordinals unique per node; the pool of attempt ids."""
    target = spec.target_rows("plan_node_attempts") or 0
    nodes = db.execute("SELECT id, plan_id, COALESCE(owner_thread_id,'thread-00000') FROM plan_nodes"
                       " WHERE state != 'pending' ORDER BY rowid").fetchall()
    rows, ordinal, open_node, open_pair = [], {}, set(), set()
    for index in range(target):
        node_id, plan_id, owner = nodes[(index * 7) % len(nodes)]
        ordinal[node_id] = ordinal.get(node_id, 0) + 1
        selected = timestamp(rng)
        # At most one open attempt per node and per (plan, executor); the rest are released.
        is_open = node_id not in open_node and (plan_id, owner) not in open_pair
        if is_open:
            open_node.add(node_id)
            open_pair.add((plan_id, owner))
        rows.append((f"attempt-{index:06d}", plan_id, node_id, owner, 1, ordinal[node_id], "fixture",
                     selected, None if is_open else selected, None if is_open else "fixture"))
    db.executemany(
        "INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,requirements_revision,"
        "attempt_ordinal,provenance,selected_at,released_at,release_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    pools.add("attempt", [r[0] for r in rows])
    return len(rows)


#: State path a node walked to reach each state: (event, prior state, new state) steps.
_PATHS = {
    "pending": (),
    "active": (("activated", "pending", "active"),),
    "review_requested": (("activated", "pending", "active"),
                         ("review_requested", "active", "review_requested")),
    "accepted": (("activated", "pending", "active"),
                 ("review_requested", "active", "review_requested"),
                 ("accepted", "review_requested", "accepted")),
    "accepted_with_followup": (("activated", "pending", "active"),
                               ("review_requested", "active", "review_requested"),
                               ("accepted_with_followup", "review_requested", "accepted_with_followup")),
    "failed": (("activated", "pending", "active"), ("failed", "active", "failed")),
    "closed": (("closed", "pending", "closed"),),
}
_REJECT = (("review_rejected", "review_requested", "active"),)


def seed_node_events(db: sqlite3.Connection, rng: random.Random) -> int:
    """Node events that follow each node's real state path, plus rejections and chatter.

    Measurement reads settlements, rejections and corrections out of this table,
    so its rows carry the same event vocabulary a node produces in use. The
    count is topped up to the census-scaled target with comment events.
    """
    nodes = db.execute("SELECT id, state, owner_thread_id, created_at FROM plan_nodes ORDER BY rowid").fetchall()
    target = spec.target_rows("plan_node_events") or 0

    def rows() -> Iterator[tuple]:
        serial = 0
        for node_id, state, owner, created in nodes:
            steps = [("created", None, "pending")] + list(_PATHS[state])
            if state.startswith("accepted") and rng.random() < 0.12:
                steps[3:3] = list(_REJECT) + [("review_requested", "active", "review_requested")]
            if rng.random() < 0.03:
                steps.append(("correction_created", state, state))
            for event, prior, new in steps:
                serial += 1
                yield (f"nodeevent-{serial:07d}", node_id, owner, "fixture", event, prior, new,
                       timestamp(rng, datetime.fromisoformat(created)))
        for extra in range(max(0, target - serial)):
            node_id, state, owner, created = nodes[rng.randrange(len(nodes))]
            yield (f"nodeevent-{serial + extra + 1:07d}", node_id, owner, "fixture", "comment", state, state,
                   timestamp(rng, datetime.fromisoformat(created)))

    return db.executemany(
        "INSERT INTO plan_node_events (id,node_id,actor_id,provenance,event,prior_state,new_state,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)", rows()).rowcount


def attribute_usage(db: sqlite3.Connection) -> int:
    """Attribute a third of each executor thread's usage rows to one of its attempts."""
    return db.execute(
        "UPDATE llm_usage SET plan_id=a.plan_id, plan_node_id=a.node_id, plan_attempt_id=a.id,"
        " plan_commitment_id=a.node_id, plan_usage_role='execution'"
        " FROM (SELECT executor_thread_id AS thread, plan_id, node_id, MIN(id) AS id"
        "       FROM plan_node_attempts GROUP BY executor_thread_id) a"
        " WHERE llm_usage.thread_id=a.thread AND llm_usage.rowid % 3 = 0").rowcount
