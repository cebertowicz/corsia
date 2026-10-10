"""A plan with a controlled amount of settled history, for the read-scale tests (#731 S2-1).

``build(db, settled=N)`` makes one owner thread's plan whose *open work is the same
for every N*: two phases and a handful of tasks (about 20 open rows) written through
the real operations, an attached worker whose own plan holds history, and a few
settled leaves from the last day. Around it sit ``N`` settled nodes, written in bulk
inside ``raw_plan_seed``: accepted, failed and closed leaves under settled phases and
under the open phase, archived nodes, and sent-back rounds, each with about seven
events, and some with attempts and usage. Several other plans share the scope. The
settled nodes are old (outside every read window), so a read that is correct costs the
same at any N.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from infra.schema import SCHEMA_SQL
from plans import mutations, service
from tests.plan_writer_authorizer import raw_plan_seed

OLD = "2025-03-01T00:00:00+00:00"
#: The viewer last saw the plan after all the settled history and before the open work's recent changes.
SEEN = "2025-06-01T00:00:00+00:00"
EVENTS_PER_NODE = 7
PHASE_WIDTH = 50


@dataclass
class Scale:
    db: sqlite3.Connection
    plan_id: str
    open_leaf: str
    #: An open container whose own work is small, whatever the history elsewhere.
    open_container: str
    #: The open container that also holds a share of the settled history: a read of it costs
    #: its subtree (its rolled cost is summed over the subtree until S5 stores it).
    history_container: str
    worker_step: str
    open_ids: list[str] = field(default_factory=list)


def _schema() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    for thread in ("owner", "w1", "w2", "o1", "o2", "o3"):
        spawned = "owner" if thread in ("w1", "w2") else None
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,spawned_by,created_at,updated_at)"
            " VALUES (?,'user-1','p',?,?,'t','t')", (thread, thread, spawned),
        )
    return db


def _task(plan_id: str, title: str, parent: str | None, db) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria="done", parent_id=parent, db=db,
    ).id


def _settle(node_id: str, db) -> None:
    service.transition_node(node_id, "active", db=db)
    mutations.request_review(node_id, evidence="proven", actor_id="owner", db=db)
    service.transition_node(
        node_id, "accepted", verification_result="passed", verified_by="t", db=db,
    )


def _open_work(db, plan_id: str) -> tuple[list[str], str, str, str, str]:
    """About 20 open rows, one attached worker, and five leaves settled just now."""
    ids: list[str] = []
    phase_a = service.add_node(
        plan_id, kind="phase", title="Phase A", acceptance_criteria="done", db=db,
    ).id
    phase_b = service.add_node(
        plan_id, kind="phase", title="Phase B", acceptance_criteria="done", db=db,
    ).id
    ids += [phase_a, phase_b]
    leaves = [_task(plan_id, f"a{i}", phase_a, db) for i in range(6)]
    leaves += [_task(plan_id, f"b{i}", phase_b, db) for i in range(6)]
    leaves += [_task(plan_id, f"c{i}", None, db) for i in range(6)]
    ids += leaves
    service.transition_node(leaves[0], "active", db=db)
    service.transition_node(leaves[1], "active", db=db)
    mutations.request_review(leaves[1], evidence="proven", actor_id="owner", db=db)
    service.add_dependency(leaves[3], leaves[2], db=db)
    service.transition_node(phase_b, "active", db=db)
    service.transition_node(leaves[7], "active", db=db)
    worker_step = leaves[12]
    recent = [_task(plan_id, f"done{i}", phase_a, db) for i in range(5)]
    for node_id in recent:
        _settle(node_id, db)
    # One failure that is the user's and that they have not seen: the chat list's failed ask.
    unseen = service.add_node(
        plan_id, kind="task", title="asks the user", acceptance_criteria="done",
        parent_id=phase_a, assignee="user", db=db,
    ).id
    service.transition_node(unseen, "active", db=db)
    service.transition_node(unseen, "failed", db=db)
    return ids, leaves[0], phase_b, phase_a, worker_step


def _worker_plan(db, step: str) -> None:
    """A worker plan with history of its own, attached under one open step."""
    worker_plan = service.ensure_plan("thread", "w1", db=db).id
    phase = service.add_node(
        worker_plan, kind="phase", title="Worker phase", acceptance_criteria="done", db=db,
    ).id
    for i in range(30):
        _settle(_task(worker_plan, f"w{i}", phase, db), db)
    _task(worker_plan, "w-open", phase, db)
    mutations.attach_spawned_thread(step, "w1", db=db)


def _other_plans(db) -> None:
    for owner in ("o1", "o2", "o3"):
        plan_id = service.ensure_plan("thread", owner, db=db).id
        phase = service.add_node(
            plan_id, kind="phase", title="Other", acceptance_criteria="done", db=db,
        ).id
        for i in range(8):
            node = _task(plan_id, f"x{i}", phase, db)
            if i % 2:
                _settle(node, db)


def _node_row(node_id, plan_id, parent, order, kind, state, commitment, *, archived=None, sent_back=0, follow_up=None):
    # Failed history is the user's, as the chat list's failed ask reads it; the viewer saw it long ago.
    assignee = "user" if state == "failed" else None
    return (
        node_id, plan_id, parent, order, kind, f"settled {node_id}", "done", state,
        commitment, OLD, OLD, archived, sent_back, follow_up, assignee,
    )


_INSERT_NODE = (
    "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,"
    "state,commitment_id,created_at,updated_at,archived_at,sent_back,follow_up_of,assignee)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_EVENT = (
    "INSERT INTO plan_node_events (id,node_id,actor_id,provenance,event,prior_state,new_state,"
    "payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
)
_EVENT_SHAPES = (
    ("node_created", None, "pending"), ("state_transition", "pending", "active"),
    ("attempt_selected", None, None), ("note", None, None),
    ("review_requested", None, None), ("state_transition", "active", "review_requested"),
    ("state_transition", "review_requested", "accepted"),
)


def _settled_rows(plan_id: str, open_phase: str, count: int):
    """Node, event, attempt and usage rows for ``count`` settled nodes, in bulk."""
    nodes: list[tuple] = []
    events: list[tuple] = []
    attempts: list[tuple] = []
    usage: list[tuple] = []
    made = 0
    phase_index = 0
    # A share of the history sits directly under the open phase; the rest under settled phases.
    under_open = count * 2 // 5
    for i in range(under_open):
        node_id = f"su{i:07d}"
        nodes.append(_node_row(node_id, plan_id, open_phase, 100 + i, "task", "accepted", node_id))
        made += 1
        events += _events(node_id, "accepted")
        if i % 5 == 0:
            attempts.append(_attempt(plan_id, node_id))
            usage.append(_usage(plan_id, node_id))
    while made < count:
        phase_index += 1
        phase_id = f"sp{phase_index:06d}"
        nodes.append(_node_row(phase_id, plan_id, None, 1000 + phase_index, "phase", "accepted", phase_id))
        made += 1
        events += _events(phase_id, "accepted")
        for j in range(min(PHASE_WIDTH, count - made)):
            node_id = f"sl{phase_index:06d}{j:03d}"
            state = ("accepted", "accepted", "failed", "closed", "accepted")[j % 5]
            archived = OLD if j % 17 == 5 else None
            nodes.append(_node_row(
                node_id, plan_id, phase_id, j, "task", state, phase_id, archived=archived,
            ))
            made += 1
            events += _events(node_id, state)
            if j % 7 == 0:
                attempts.append(_attempt(plan_id, node_id))
                usage.append(_usage(plan_id, node_id))
            if j % 23 == 3 and made < count:
                # A sent-back round: closed beside the revision that carries the work.
                round_id = f"sb{phase_index:06d}{j:03d}"
                nodes.append(_node_row(
                    round_id, plan_id, phase_id, 900 + j, "task", "closed", phase_id, sent_back=1,
                ))
                made += 1
                events += _events(round_id, "closed", rejected=True)
    return nodes, events, attempts, usage


def _events(node_id: str, state: str, *, rejected: bool = False):
    rows = []
    for k, (event, prior, new) in enumerate(_EVENT_SHAPES):
        name = event
        if k == len(_EVENT_SHAPES) - 1:
            new = state
            if rejected:
                name = "review_rejected"
        rows.append((
            f"e{node_id}{k}", node_id, "owner", "seed", name, prior, new,
            '{"feedback": "again"}' if rejected and name == "review_rejected" else None,
            f"2025-03-01T00:00:{k:02d}+00:00",
        ))
    return rows


def _attempt(plan_id: str, node_id: str):
    return (
        f"at{node_id}", plan_id, node_id, "owner", 1, 1, "owner", "seed", OLD, OLD, "completed",
    )


def _usage(plan_id: str, node_id: str):
    return (
        f"us{node_id}", "owner", "m", 10, 20, 30, 0.01, plan_id, node_id, f"at{node_id}",
        node_id, "execution", OLD,
    )


def build(settled: int) -> Scale:
    db = _schema()
    plan_id = service.ensure_plan("thread", "owner", db=db).id
    open_ids, open_leaf, open_container, history_container, worker_step = _open_work(db, plan_id)
    _worker_plan(db, worker_step)
    _other_plans(db)
    db.execute(
        "INSERT INTO plan_pane_views (user_id,thread_id,seen_through,updated_at) VALUES (?,?,?,?)",
        ("user-1", "owner", SEEN, SEEN),
    )
    db.commit()
    nodes, events, attempts, usage = _settled_rows(plan_id, history_container, settled)
    with raw_plan_seed(db):
        db.executemany(_INSERT_NODE, nodes)
        db.executemany(_INSERT_EVENT, events)
        db.executemany(
            "INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,"
            "requirements_revision,attempt_ordinal,selected_by_actor_id,provenance,selected_at,"
            "released_at,release_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)", attempts,
        )
        db.executemany(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,"
            "total_tokens,cost_usd,plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,"
            "plan_usage_role,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", usage,
        )
    db.commit()
    return Scale(db, plan_id, open_leaf, open_container, history_container, worker_step, open_ids)
