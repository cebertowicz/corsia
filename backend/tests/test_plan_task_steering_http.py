"""Right-click a plan task: the user pauses, resumes or closes any open task.

Drives the exact requests the pane posts to the mutations route. Pause places a
user-owned hold, only the user resumes it, closing keeps the task on the plan
with its reason, and the owning agent learns through its passive digest. A task
handed to a live worker is paused as the worker's whole plan instead.
"""

from __future__ import annotations

import json

import pytest

from plans import operator_projection, user_steering
from plans.boundary import PlanActor, mutate
from tests.plan_ask_helpers import document_ask
from tests.plan_execution_helpers import agent_pause
from tests.test_attention_queue import _mid
from tests.test_plan_archival import OWNER, _plan, _row, _task, db  # noqa: F401 - fixture
from tests.test_plan_user_action_http import _client

FIELD_DEFAULT_REASON = "Closed by the user"
USER = PlanActor(None, "user-1", None, kind="user")


def _post(client, plan_id: str, operation: str, node, **extra):
    revision = _row(client.db, node.id)["revision"]
    return client.post(
        f"/api/plans/{plan_id}/mutations",
        json={
            "operation": operation,
            "mutation_id": f"{operation}:{node.id}:{revision}",
            "params": {"node_id": node.id, "expected_revision": revision, **extra},
        },
    )


@pytest.fixture
def client(db, monkeypatch):
    http = _client(db, monkeypatch)
    http.db = db
    return http


def _holds(db, node_id: str) -> list:
    return db.execute(
        "SELECT * FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node_id,),
    ).fetchall()


def _digest(db, plan_id: str) -> list[str]:
    return [
        row["summary"] for row in db.execute(
            """SELECT summary FROM context_change_journal
               WHERE object_kind='plan' AND object_id=? AND change_kind='plan.user_task_action'
               ORDER BY seq""",
            (plan_id,),
        ).fetchall()
    ]


def _delegated(db, plan_id: str, worker: str = "child"):
    """A task handed to a live worker, and that worker's plan under this one."""
    node = _task(db, plan_id, title="Delegated work")
    worker_plan = _plan(db, worker)
    db.execute(
        "UPDATE plan_nodes SET state='active', spawned_thread_id=? WHERE id=?",
        (worker, node.id),
    )
    db.execute("UPDATE plans SET parent_plan_id=? WHERE id=?", (plan_id, worker_plan.id))
    db.commit()
    return node, worker_plan


def _plan_state(db, plan_id: str) -> str:
    return db.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()["state"]


def _plan_move(db, plan_id: str, operation: str) -> None:
    revision = db.execute("SELECT revision FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
    mutate(plan_id, operation, {"expected_revision": revision},
           actor=USER, provenance="test", mutation_id=_mid(), db=db)
    db.commit()


def _row_in_pane(db, node_id: str) -> dict:
    """The node exactly as the plan pane receives it."""
    projected = operator_projection.read_thread("root", actor=USER, db=db)["plans"][0]
    return next(node for node in projected["nodes"] if node["id"] == node_id)


@pytest.mark.parametrize("assignee", [None, "user"])
def test_the_user_pauses_and_resumes_any_open_task(client, db, assignee):
    plan = _plan(db)
    node = _task(db, plan.id, title="Work", assignee=assignee)

    paused = _post(client, plan.id, "pause_node", node)

    assert paused.status_code == 200, paused.text
    (hold,) = _holds(db, node.id)
    assert hold["kind"] == "paused" and hold["owner_id"].startswith("user:")
    assert user_steering.is_user_pause(hold)
    assert _row_in_pane(db, node.id)["user_actions"][-2:] == ["resume_node", "close_node"]

    resumed = _post(client, plan.id, "resume_node", node)

    assert resumed.status_code == 200, resumed.text
    assert _holds(db, node.id) == []
    assert _row(db, node.id)["state"] in {"pending", "active"}


def test_the_user_closes_any_open_task_with_a_default_or_given_reason(client, db):
    plan = _plan(db)
    plain = _task(db, plan.id, title="Plain")
    given = _task(db, plan.id, title="Given")

    assert _post(client, plan.id, "close_node", plain).status_code == 200
    assert _post(client, plan.id, "close_node", given, reason="Not needed now").status_code == 200

    assert _row(db, plain.id)["state"] == "closed"
    assert _row(db, given.id)["state"] == "closed"
    reasons = {
        row["node_id"]: row["payload"] for row in db.execute(
            "SELECT node_id, payload_json AS payload FROM plan_node_events WHERE new_state='closed'",
        ).fetchall()
    }
    assert FIELD_DEFAULT_REASON in reasons[plain.id]
    assert "Not needed now" in reasons[given.id]


def test_the_user_resumes_a_pause_an_agent_left(client, db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Set aside")
    # Placed before agents lost pause, and one no schema step moved (another
    # thread's, or on a scope plan): nothing else would ever end it.
    agent_pause(db, node.id, "Waiting on a design", thread_id="another-thread")

    response = _post(client, plan.id, "resume_node", node)

    assert response.status_code == 200, response.text
    assert _holds(db, node.id) == []


@pytest.mark.parametrize("state", ["accepted", "closed", "failed"])
def test_a_finished_task_refuses_every_steering_action(client, db, state):
    plan = _plan(db)
    node = _task(db, plan.id, title="Done")
    db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (state, node.id))
    db.commit()

    for operation in ("pause_node", "close_node"):
        assert _post(client, plan.id, operation, node).status_code == 409


def test_pausing_a_delegated_task_pauses_its_worker_plan_and_places_no_hold(client, db):
    plan = _plan(db)
    node, worker = _delegated(db, plan.id)

    assert _row_in_pane(db, node.id)["user_actions"] == ["mark_done", "pause_node", "close_node"]
    assert _post(client, plan.id, "pause_node", node).status_code == 200

    assert _plan_state(db, worker.id) == "paused"
    assert _holds(db, node.id) == []
    assert _row_in_pane(db, node.id)["user_actions"] == ["mark_done", "resume_node", "close_node"]

    assert _post(client, plan.id, "resume_node", node).status_code == 200
    assert _plan_state(db, worker.id) == "active"
    assert _row_in_pane(db, node.id)["user_actions"] == ["mark_done", "pause_node", "close_node"]


def test_resuming_the_task_keeps_a_worker_plan_the_user_paused_on_its_own(client, db):
    """Nothing hidden releases a worker pause: the worker's plan resumes only
    when the user asks for exactly that, from its task or its own pane."""
    plan = _plan(db)
    node, worker = _delegated(db, plan.id)

    # The user pauses the worker's own plan directly, through its own pane.
    _plan_move(db, worker.id, "pause")

    # The task shows that pause: Resume, not a second Pause.
    assert _row_in_pane(db, node.id)["user_actions"] == ["mark_done", "resume_node", "close_node"]
    assert _post(client, plan.id, "pause_node", node).status_code == 409
    assert _plan_state(db, worker.id) == "paused"
    assert _holds(db, node.id) == []

    # Only the user's own Resume on the task moves the worker's plan.
    assert _post(client, plan.id, "resume_node", node).status_code == 200
    assert _plan_state(db, worker.id) == "active"


def test_a_delegated_task_offers_resume_only_while_its_own_plan_is_active(client, db):
    plan = _plan(db)
    node, worker = _delegated(db, plan.id)
    _plan_move(db, plan.id, "pause")
    assert _plan_state(db, worker.id) == "paused"

    assert _row_in_pane(db, node.id)["user_actions"] == ["mark_done", "close_node"]
    assert _post(client, plan.id, "resume_node", node).status_code == 409
    assert _plan_state(db, worker.id) == "paused"


def test_a_reused_worker_is_steered_only_from_the_task_showing_it(client, db):
    """The menu and the server read one attachment: the newest one wins."""
    from plans import service

    plan = _plan(db)
    older, worker = _delegated(db, plan.id)
    newer = _task(db, plan.id, title="Follow-up")
    db.execute(
        "UPDATE plan_nodes SET state='active', spawned_thread_id='child' WHERE id=?",
        (newer.id,),
    )
    for node_id in (older.id, newer.id):
        service._event(
            db, node_id, "spawned_thread_attached", actor_id=None, provenance="test",
            payload={"child_plan_id": worker.id},
        )
    db.commit()

    assert user_steering.worker_plan(db, _row(db, older.id)) is None
    assert user_steering.worker_plan(db, _row(db, newer.id))["id"] == worker.id
    assert _row_in_pane(db, older.id)["child"] is None
    assert _row_in_pane(db, newer.id)["child"] is not None

    response = _post(client, plan.id, "pause_node", older)

    # The older task is paused as a task; the worker's plan belongs to the newer.
    assert response.status_code == 200, response.text
    assert _plan_state(db, worker.id) == "active"
    assert len(_holds(db, older.id)) == 1


def test_the_worker_plan_pause_tells_the_delegating_agent_once(client, db):
    """The ordinary parent notice carries it; the task path adds no second line."""
    plan = _plan(db)
    node, _worker = _delegated(db, plan.id)

    assert _post(client, plan.id, "pause_node", node).status_code == 200

    kinds = [
        row["change_kind"] for row in db.execute(
            "SELECT change_kind FROM context_change_journal WHERE object_id=?", (plan.id,),
        ).fetchall()
    ]
    assert kinds.count("plan.child_plan_paused") == 1
    assert user_steering.CHANGE_KIND not in kinds


def test_a_user_pause_hold_is_owned_by_the_user(client, db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Work")

    assert _post(client, plan.id, "pause_node", node).status_code == 200

    (hold,) = _holds(db, node.id)
    assert hold["owner_kind"] == "user"


def test_an_agent_pause_hold_stays_owned_by_its_thread(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Set aside")
    agent_pause(db, node.id, "Waiting on a design", thread_id=OWNER.thread_id)

    (hold,) = _holds(db, node.id)
    assert (hold["owner_kind"], hold["owner_id"]) == ("thread", "root")
    assert not user_steering.is_user_pause(hold)


@pytest.mark.parametrize(
    ("hold", "mine"),
    [
        ({"kind": "paused", "owner_kind": "user", "owner_id": "user:user-1"}, True),
        # Schema 59 records every older user pause as owner_kind='user'; a
        # thread-owned pause is the agent's, whatever its owner id reads.
        ({"kind": "paused", "owner_kind": "thread", "owner_id": "user:user-1"}, False),
        ({"kind": "paused", "owner_kind": "thread", "owner_id": "root"}, False),
        ({"kind": "blocked", "owner_kind": "user", "owner_id": None}, False),
    ],
)
def test_a_user_pause_is_read_from_the_hold_owner(hold, mine):
    assert user_steering.is_user_pause(hold) is mine


def test_a_user_owned_pause_reads_the_same_on_the_row_as_an_agent_pause(client, db):
    """owner_kind='user' on a pause changes who may resume it, not its colour."""
    plan = _plan(db)
    by_user = _task(db, plan.id, title="Paused by the user")
    by_agent = _task(db, plan.id, title="Paused by the agent")
    assert _post(client, plan.id, "pause_node", by_user).status_code == 200
    agent_pause(db, by_agent.id, "Paused by the user", thread_id=OWNER.thread_id)

    user_row, agent_row = _row_in_pane(db, by_user.id), _row_in_pane(db, by_agent.id)

    for field in ("display", "status_reason", "requires_user_action"):
        assert user_row[field] == agent_row[field], field
    assert user_row["execution"]["condition"] == agent_row["execution"]["condition"] == "paused"


def test_in_a_paused_plan_the_menu_reads_only_the_task_s_own_pause(client, db):
    """'Plan is paused' is derived, not a hold: the user's paused task keeps
    Resume, and a task nobody paused still offers Pause."""
    plan = _plan(db)
    mine = _task(db, plan.id, title="Paused by the user")
    free = _task(db, plan.id, title="Not paused")
    assert _post(client, plan.id, "pause_node", mine).status_code == 200
    _plan_move(db, plan.id, "pause")

    assert _row_in_pane(db, mine.id)["user_actions"] == ["mark_done", "resume_node", "close_node"]
    assert _row_in_pane(db, free.id)["user_actions"] == ["mark_done", "pause_node", "close_node"]
    assert _post(client, plan.id, "resume_node", mine).status_code == 200
    assert _holds(db, mine.id) == []


def test_a_thread_cannot_resume_a_task_the_user_paused(client, db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Held")
    assert _post(client, plan.id, "pause_node", node).status_code == 200
    (hold,) = _holds(db, node.id)

    with pytest.raises(Exception, match="The user paused this task; only they resume it"):
        mutate(plan.id, "resume_node", {
            "node_id": node.id, "hold_id": hold["id"],
            "expected_revision": _row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)

    assert len(_holds(db, node.id)) == 1


def test_a_thread_may_still_resolve_a_blocker_the_user_owns(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Needs Steve")
    mutate(plan.id, "block_node", {
        "node_id": node.id, "expected_revision": _row(db, node.id)["revision"],
        "owner_kind": "user", "reason": "Needs Steve", "reason_key": "needs_user",
        "ask_artifact_id": document_ask(db, "root"),
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)
    (hold,) = _holds(db, node.id)

    mutate(plan.id, "resolve_blocker", {
        "node_id": node.id, "hold_id": hold["id"],
        "expected_revision": _row(db, node.id)["revision"],
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db)

    assert _holds(db, node.id) == []


def test_the_owning_agent_is_told_in_its_digest_after_a_pause_and_a_close(client, db):
    plan = _plan(db)
    paused = _task(db, plan.id, title="Write the report")
    closed = _task(db, plan.id, title="Migrate the table")

    _post(client, plan.id, "pause_node", paused)
    _post(client, plan.id, "close_node", closed, reason="Out of scope")

    lines = _digest(db, plan.id)
    from infra.passive_changes import materialize_pending_digest

    told = materialize_pending_digest(db, "root")
    assert told is not None
    # A digest the close write itself materialized already carries the status
    # that write left, so read every digest the owner was handed.
    delivered = "\n".join(row["content"] for row in db.execute(
        "SELECT content FROM messages WHERE thread_id='root' AND id LIKE 'context-digest:%' "
        "ORDER BY rowid",
    ))
    assert "Write the report" in delivered and "Out of scope" in delivered
    assert "1 paused by the user" in delivered and '"title":"Write the report"' in delivered
    assert any("paused" in line and "Write the report" in line and "do not work it" in line
               for line in lines)
    assert any("closed" in line and "Migrate the table" in line and "Out of scope" in line
               for line in lines)


def test_the_owner_line_names_the_user_and_the_event_that_caused_it(client, db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Write the report")

    _post(client, plan.id, "pause_node", node)

    (row,) = db.execute(
        """SELECT actor_thread_id, details_json FROM context_change_journal
           WHERE change_kind=?""", (user_steering.CHANGE_KIND,),
    ).fetchall()
    placed = db.execute(
        "SELECT id FROM plan_node_events WHERE node_id=? AND event='hold_placed'", (node.id,),
    ).fetchone()["id"]
    assert row["actor_thread_id"] == USER.audit_id
    assert json.loads(row["details_json"])["source_event_id"] == placed


def test_an_archived_owner_is_not_told(client, db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Write the report")
    db.execute("UPDATE threads SET archived_at='t' WHERE id='root'")
    db.commit()

    assert _post(client, plan.id, "pause_node", node).status_code == 200

    assert _digest(db, plan.id) == []


_USER_PAUSE = {"placed": True, "kind": "paused", "owner_kind": "user",
               "owner_id": "user:user-1"}
_AGENT_PAUSE = {"placed": True, "kind": "paused", "owner_kind": "thread", "owner_id": "root"}
#: Derived causes the projection lists alongside holds; neither is a hold.
_PLAN_PAUSED = {"kind": "paused", "reason_key": "plan_paused", "owner_kind": "thread",
                "owner_id": "root"}
_WORKER_PAUSED = {"kind": "paused", "reason_key": "delegated_child_paused",
                  "owner_kind": "thread", "owner_id": "child"}


@pytest.mark.parametrize(
    ("node", "causes", "plan_state", "worker", "actions"),
    [
        ({"kind": "task", "assignee": None, "state": "pending"}, [], "active", None,
         ["mark_done", "pause_node", "close_node"]),
        ({"kind": "task", "assignee": None, "state": "active"}, [_USER_PAUSE], "active", None,
         ["mark_done", "resume_node", "close_node"]),
        ({"kind": "task", "assignee": None, "state": "active"}, [_AGENT_PAUSE], "active", None,
         ["mark_done", "resume_node", "close_node"]),
        ({"kind": "task", "assignee": "user", "state": "pending"}, [], "active", None,
         ["mark_done", "pause_node", "close_node"]),
        ({"kind": "task", "assignee": "user", "state": "active"}, [_USER_PAUSE], "active",
         None, ["mark_done", "resume_node", "close_node"]),
        # A paused plan's derived cause comes first; only the placed hold counts.
        ({"kind": "task", "assignee": None, "state": "active"},
         [_PLAN_PAUSED, _USER_PAUSE], "paused", None, ["mark_done", "resume_node", "close_node"]),
        ({"kind": "task", "assignee": None, "state": "pending"}, [_PLAN_PAUSED], "paused",
         None, ["mark_done", "pause_node", "close_node"]),
        # A delegated task follows its worker's plan, not the derived cause.
        ({"kind": "task", "assignee": None, "state": "active"}, [], "active", "active",
         ["mark_done", "pause_node", "close_node"]),
        ({"kind": "task", "assignee": None, "state": "active"}, [_WORKER_PAUSED], "active",
         "paused", ["mark_done", "resume_node", "close_node"]),
        ({"kind": "task", "assignee": None, "state": "active"},
         [_PLAN_PAUSED, _WORKER_PAUSED], "paused", "paused", ["mark_done", "close_node"]),
        ({"kind": "task", "assignee": None, "state": "accepted"}, [], "active", None, []),
        ({"kind": "task", "assignee": None, "state": "closed"}, [], "active", None, []),
        # A phase settles like a task but has no pause of its own.
        ({"kind": "phase", "assignee": None, "state": "pending"}, [], "active", None,
         ["mark_done", "close_node"]),
    ],
)
def test_the_pane_offers_the_actions_the_row_state_allows(
    node, causes, plan_state, worker, actions,
):
    assert user_steering.offered(
        node, None, causes, plan_state=plan_state, worker_plan_state=worker,
    ) == actions


def test_resume_and_close_stay_agent_operations_and_pause_is_the_users(db):
    """Adding the human path must not take resume or close from the agent tool.

    Pause is the user's alone: an agent schedules a task instead.
    """
    from plans import boundary_fields

    assert not {"resume_node", "close_node"} & boundary_fields.HUMAN_OPERATIONS
    assert {"pause_node", "pause"} <= boundary_fields.HUMAN_OPERATIONS
