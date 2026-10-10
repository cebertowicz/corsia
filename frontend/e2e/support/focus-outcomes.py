"""Seed synthetic outcome proof only in e2e-local's disposable database. No provider calls."""
import json
import os
from pathlib import Path
import sqlite3
import sys

from infra import thread_updates
from plans import service


def guarded_db():
    assert os.environ.get("CORSIA_E2E_MOCK") == "1"
    assert os.environ.get("CORSIA_TEST_MODE") == "1"
    state = Path(os.environ["CORSIA_E2E_STATE_DIR"]).resolve()
    workspace = Path(os.environ["CORSIA_E2E_WORKSPACE"]).resolve()
    assert state == Path(os.environ["CORSIA_STATE_DIR"]).resolve()
    assert workspace == Path(os.environ["CORSIA_WORKSPACE"]).resolve()
    assert state.name == "e2e-state" and workspace.name == "e2e-workspace"
    assert state.parent == workspace.parent and state.parent.name.startswith("corsia-focus-e2e-")
    assert state.parent.parent == Path("/tmp").resolve()
    assert (state / "corsia.db").is_file()
    db = sqlite3.connect(state / "corsia.db")
    db.row_factory = sqlite3.Row
    return db


def message(db, thread, seq, role, content, turn, at, *, marked=False):
    metadata = {"turn_id": turn} if role == "assistant" else {}
    if marked:
        metadata["for_user"] = True
    identifier = f"{thread}-proof-{seq}"
    db.execute(
        "INSERT INTO messages (id,thread_id,seq,role,content,metadata,created_at,"
        "active_turn_id,delivery_state,write_started_at,write_outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (identifier, thread, seq, role, content, json.dumps(metadata), at,
         turn if role == "user" else None, "delivered", at, "accepted"),
    )
    return identifier


def turn(db, thread, identifier, paused=False):
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,error,"
        "started_at,ended_at,boot_epoch) VALUES (?,?,0,'mock/test-model',?,?,?,?,'e2e')",
        (identifier, thread, "cancelled" if paused else "completed",
         "plan_limit_overage_off" if paused else None, "2026-09-29T09:00:00", "2026-09-29T11:10:00"),
    )


def node(db, plan, title, assignee="coordinator", state="accepted"):
    row = service.add_node(plan, kind="task", title=title, acceptance_criteria="Synthetic proof", assignee=assignee, db=db)
    db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (state, row.id))
    if state == "failed":
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,event,prior_state,new_state,provenance,created_at) "
            "VALUES (?,?,'state_transition','active','failed','e2e fixture','2026-09-29T11:00:00')",
            (f"{row.id}-failure", row.id),
        )
    return row.id


def outcome(db, thread, node_id, kind, seq, at, event):
    thread_updates.record_plan_update(db, thread_id=thread, kind=kind, event_id=f"{thread}-{event}", node_id=node_id, created_at=at)
    db.execute("UPDATE thread_updates SET after_seq=? WHERE source_id=?", (seq, f"{thread}-{event}"))


def seed(db, main, passive, failed):
    ids = [main, passive, failed]
    owners = [db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone() for thread in ids]
    assert all(row is not None for row in owners) and len({row[0] for row in owners}) == 1
    user = owners[0][0]
    assert not db.execute("SELECT 1 FROM messages WHERE thread_id IN (?,?,?)", ids).fetchone()
    p, n, q = (f"{main}-{suffix}" for suffix in ("paused", "next", "new"))
    turn(db, main, p, paused=True)
    turn(db, main, n)
    turn(db, main, q)
    specs = [
        (1, "assistant", "Stopped work one", p, "2026-09-29T09:00:00"),
        (2, "assistant", "Stopped work two", p, "2026-09-29T09:01:00"),
        (3, "assistant", "Work after pause", n, "2026-09-29T09:02:00"),
        (4, "user", "Keep the original scope", n, "2026-09-29T09:03:00"),
        (5, "assistant", "New work one", q, "2026-09-29T11:00:00"),
        (6, "assistant", "New work two", q, "2026-09-29T11:01:00"),
        (7, "assistant", "Meaningful decision update", q, "2026-09-29T11:02:00"),
        (8, "assistant", "Work after failure", q, "2026-09-29T11:03:00"),
    ]
    for seq, role, text, owner, at in specs:
        msg = message(db, main, seq, role, text, owner, at, marked=seq == 7)
        if seq == 7:
            thread_updates.record_marked(db, thread_id=main, message_id=msg, node_id=None, created_at=at)
    plan = service.ensure_plan("thread", main, db=db).id
    tasks = [
        ("Original completed snapshot with wrapping on a narrow screen", "done", 1, "2026-09-29T09:00:01.123456"),
        ("Follow-up snapshot", "done_follow_up", 2, "2026-09-29T09:00:01.654321"),
        ("Deleted closed snapshot", "closed", 3, "2026-09-29T09:02:01"),
        ("New completed snapshot", "done", 5, "2026-09-29T11:00:01"),
        ("New follow-up snapshot", "done_follow_up", 6, "2026-09-29T11:01:01"),
        ("Failure assigned to you", "failed", 7, "2026-09-29T11:02:01"),
        ("Latest completed snapshot", "done", 8, "2026-09-29T11:03:01"),
        # Failures on items the agent owns: quiet members of the nearest passive list.
        ("Agent-owned failure before the visit", "failed", 3, "2026-09-29T09:02:02"),
        ("Agent-owned failure after the visit", "failed", 8, "2026-09-29T11:02:30"),
    ]
    for index, (title, kind, seq, at) in enumerate(tasks):
        item = node(db, plan, title, "user" if title == "Failure assigned to you" else "coordinator", state={"failed": "failed", "closed": "closed", "done_follow_up": "accepted_with_followup"}.get(kind, "accepted"))
        outcome(db, main, item, kind, seq, at, f"outcome-{index}")
        if index == 0:
            db.execute("UPDATE plan_nodes SET title='Renamed after outcome' WHERE id=?", (item,))
        if kind == "closed":
            db.execute("DELETE FROM plan_nodes WHERE id=?", (item,))
    live_close = node(db, plan, "Live closure snapshot", state="pending")
    db.execute(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,last_exited_at,last_confirmed_at,updated_at) VALUES (?,?,0,?,?,?,?)",
        (user, main, "2026-09-29T08:00:00", "2026-09-29T10:00:00", "2026-09-29T10:00:00", "2026-09-29T10:00:00"),
    )
    passive_plan = service.ensure_plan("thread", passive, db=db).id
    passive_node = node(db, passive_plan, "Passive sidebar snapshot")
    outcome(db, passive, passive_node, "closed", 0, "2026-09-29T11:00:00", "closed-only")
    failed_plan = service.ensure_plan("thread", failed, db=db).id
    for assignee in ("user", "coordinator", "subagent", None):
        node(db, failed_plan, f"Failure assigned to {assignee or 'nobody'}", assignee, "failed")
    db.commit()
    revision = db.execute("SELECT revision FROM plan_nodes WHERE id=?", (live_close,)).fetchone()[0]
    times = [row[0] for row in db.execute("SELECT created_at FROM thread_updates WHERE thread_id=? AND source_kind='plan_event' ORDER BY after_seq, id", (main,))]
    return {"plan": plan, "live_close": live_close, "live_revision": revision, "times": times, "user": user}


if __name__ == "__main__":
    with guarded_db() as connection:
        print(json.dumps(seed(connection, *sys.argv[1:4])))
