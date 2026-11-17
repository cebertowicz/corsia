"""A synthetic thread shaped like a busy one: many cards over a large ``plan_nodes`` table."""

from __future__ import annotations

import json
import random

from tests.plan_writer_authorizer import raw_plan_seed
from tests.presence_helpers import new_thread

KINDS = ("done", "done_follow_up", "failed", "closed", "blocked", "person_step")
STATES = ("pending", "active", "review_requested", "accepted", "accepted_with_followup", "failed", "closed")


def seed_cards(db, *, thread_id: str = "focus", nodes: int, cards: int, seed: int = 7) -> None:
    """Cards over a random forest: failed/blocked/user-owned items, retired branches, follow-ups,
    deleted and archived nodes, asks open and cleared, and person steps whose node is gone."""
    rnd = random.Random(seed)
    new_thread(db, thread_id)
    rows, holds, events, open_nodes = [], [], [], []
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plans (id,owner_kind,owner_id,created_at,updated_at)"
            " VALUES ('p1','thread',?,'t','t')", (thread_id,))
        for i in range(nodes):
            parent = rnd.choice(open_nodes) if open_nodes and rnd.random() < 0.7 else None
            follow_up = f"n{rnd.randrange(i)}" if i > 5 and rnd.random() < 0.05 else None
            state = rnd.choice(STATES)
            if state in ("pending", "active", "review_requested"):
                open_nodes.append(f"n{i}")
            rows.append((
                f"n{i}", "p1", parent, i, "task", f"T{i}", "ac", state,
                rnd.choice(("user", None, None)), f"n{i}", follow_up,
                None, "t", "t"))
        db.executemany(
            "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,"
            "state,assignee,commitment_id,follow_up_of,archived_at,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    updates = []
    for n in range(cards):
        kind = rnd.choice(KINDS)
        # Some updates name a node that no longer exists.
        node = f"n{rnd.randrange(nodes)}" if rnd.random() > 0.1 else f"gone{n}"
        source = f"ev{n}"
        if kind == "blocked" and rnd.random() < 0.6:
            hold = f"h{n}"
            events.append((source, node if node.startswith("n") and not node.startswith("gone") else "n0",
                           json.dumps({"hold_id": hold})))
            holds.append((hold, events[-1][1], rnd.choice(("user", "thread")),
                          rnd.choice((f"art{n}", None)), rnd.random() < 0.3))
        updates.append((thread_id, kind, "plan_event", source, node, f"Title {n}", n // 3,
                        f"2026-01-01T{n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}.{n:06d}"))
    with raw_plan_seed(db):
        db.commit()
        for event_id, node_id, payload in events:
            db.execute(
                "INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at)"
                " VALUES (?,?,?,?,?,?)", (event_id, node_id, "test", "hold_placed", payload, "t"))
        for hold_id, node_id, owner_kind, owner_id, cleared in holds:
            cleared_cols = ("a", "t", events[0][0]) if cleared else (None, None, None)
            db.execute(
                "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,owner_id,"
                "provenance,created_at,cleared_by_actor_id,cleared_at,clearance_event_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (hold_id, node_id, "blocked", "k", "r", owner_kind, owner_id, "test", "t", *cleared_cols))
        db.commit()
    db.executemany(
        "INSERT INTO thread_updates (thread_id,kind,source_kind,source_id,node_id,title,after_seq,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)", updates)
    db.commit()
    with raw_plan_seed(db):
        pass
    db.commit()
