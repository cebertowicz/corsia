"""The person's pile shows a document ask exactly while its hold is open.

Integration journeys through the public plan (``mutate``) and registry
(``attention``, ``dispose``, ``watches``) entry points, read back through the
pile's own routes (``/raised``, ``/rollup``, ``/queue``) and the chat list's
needs-you items.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, service
from plans.boundary import PlanActor, mutate

AGENT = PlanActor("owner", "user", "project")
PERSON = PlanActor(None, "user", "project", kind="user")
#: Where an open document ask shows: ``/raised``, the owner's ``/rollup`` and
#: ``list_raised``. ``/queue`` lists typed renderers only and is checked for absence.
OPEN_ASK_ROUTES = {"raised", "rollup", "list_raised"}


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,title,model,created_at,updated_at)
           VALUES ('owner','user','project','Owner','test/model','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _document(db, external_id: str) -> str:
    from registry import models

    models.register_artifact(
        integration="workspace", type="document", external_id=external_id,
        parent_thread_id="owner", title="Read the proposal", db=db,
    )
    db.commit()
    return str(db.execute(
        "SELECT id FROM artifact_identities WHERE external_id=?", (external_id,),
    ).fetchone()["id"])


def _plan_op(db, plan_id: str, operation: str, params: dict, actor: PlanActor = AGENT):
    node_id = params["node_id"]
    return mutate(
        plan_id, operation,
        {**params, "expected_revision": service._node_row(db, node_id)["revision"]},
        actor=actor, provenance="test.ask_pile", mutation_id=uuid.uuid4().hex, db=db,
    )


def _ask(db, plan_id: str, node_id: str, artifact_id: str) -> str:
    _plan_op(db, plan_id, "block_node", {
        "node_id": node_id, "reason_key": "review", "reason": "Review it",
        "owner_kind": "user", "ask_artifact_id": artifact_id,
    })
    return str(db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node_id,),
    ).fetchone()["id"])


def _asked(db, external_id: str = "proposal.md"):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    artifact_id = _document(db, external_id)
    return plan.id, node.id, artifact_id, _ask(db, plan.id, node.id, artifact_id)


def _withdraw(db, plan_id: str, node_id: str, hold_id: str) -> None:
    _plan_op(db, plan_id, "resolve_blocker", {"node_id": node_id, "hold_id": hold_id})


def _pile(db) -> dict[str, set[str]]:
    """Each pile route's artifacts, as the person's client reads them."""
    from api.attention import _queue_for, _summarize_raised
    from registry import attention

    return {
        "raised": {row["external_id"] for row in _summarize_raised(None, "user", db=db)},
        "rollup": {row["external_id"] for row in _summarize_raised("owner", "user", db=db)},
        "queue": {row["external_id"] for row in _queue_for("user", db=db)},
        "list_raised": {row["external_id"] for row in attention.list_raised(db=db)},
    }


def _in_pile(db, external_id: str) -> set[str]:
    return {route for route, ids in _pile(db).items() if external_id in ids}


def _needs_you(db) -> list[dict]:
    from plans.operator_chat_state import read_thread_states

    state = read_thread_states(["owner"], actor=PERSON, db=db)["states"].get("owner", {})
    return state.get("attention_items", [])


def _ended(db, hold_id: str) -> dict | None:
    payload = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE id=("
        "SELECT clearance_event_id FROM plan_node_holds WHERE id=?)", (hold_id,),
    ).fetchone()["payload_json"]
    return json.loads(payload).get("detail")


def _card(db, hold_id: str, artifact_id: str) -> str:
    from registry.chat_cards import ask_cards

    return ask_cards(db, {hold_id: artifact_id})[hold_id]["state"]


def _asker_inputs(db) -> list[str]:
    """Every waking input the asker's FIFO took: plan wakes and owner wakes alike."""
    wakes = [
        str(row["id"]) for row in db.execute(
            "SELECT id FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:%'",
        ).fetchall()
    ]
    owner_wakes = [
        str(row["seq"]) for row in db.execute(
            "SELECT seq FROM context_change_journal WHERE change_kind='artifact.disposition'",
        ).fetchall()
    ]
    return wakes + owner_wakes


def test_an_open_ask_is_in_the_pile_and_needs_you(db):
    _plan_id, node_id, _artifact, _hold = _asked(db)

    # The queue draws typed renderers only; a document ask is a generic pile row.
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES
    assert [item["node_id"] for item in _needs_you(db)] == [node_id]


# The agent withdraws, the task closes, the task is archived. The pile and
# needs-you change in the ending's own transaction.
@pytest.mark.parametrize(("ending", "ended", "card"), [
    ("withdraw", "withdrawn", "withdrawn"),
    ("close_node", "closed", "closed"),
    ("archive_node", "closed", "closed"),
])
def test_an_ended_ask_leaves_the_pile_and_needs_you_in_the_same_transaction(
    db, ending, ended, card,
):
    plan_id, node_id, artifact_id, hold_id = _asked(db)
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES

    # The test owns the transaction, so the ending nests in it and is rolled back
    # below. Its post-commit nudge then has no commit to follow and logs that.
    db.execute("BEGIN")
    if ending == "withdraw":
        _withdraw(db, plan_id, node_id, hold_id)
    elif ending == "close_node":
        _plan_op(db, plan_id, "close_node", {"node_id": node_id, "reason": "Not needed"}, PERSON)
    else:
        _plan_op(db, plan_id, "archive_node", {"node_id": node_id}, PERSON)
    assert db.in_transaction, "the ending must not have committed on its own"
    assert _in_pile(db, "proposal.md") == set()
    assert _needs_you(db) == []
    assert _ended(db, hold_id) == {"ended": ended}
    assert _card(db, hold_id, artifact_id) == card
    db.rollback()

    # Nothing outside that write removed it: rolled back, the ask is live again.
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES
    assert [item["node_id"] for item in _needs_you(db)] == [node_id]


def test_an_agent_withdrawal_releases_the_task_and_wakes_nobody(db):
    plan_id, node_id, artifact_id, hold_id = _asked(db)

    _withdraw(db, plan_id, node_id, hold_id)

    assert admission.project(node_id, db=db)["condition"] != "blocked"
    assert _asker_inputs(db) == []
    assert _card(db, hold_id, artifact_id) == "withdrawn"


# Structured Plans' #841 review: a deleted or purged worker's open ask ends with it.
def test_a_deleted_threads_ask_leaves_the_pile_and_wakes_nobody(db):
    from plans.delegation_child_lifecycle import on_child_thread_ended

    plan_id, node_id, artifact_id, hold_id = _asked(db)

    on_child_thread_ended(db, "owner", reason="deleted")

    assert db.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()["state"] == "archived"
    assert _in_pile(db, "proposal.md") == set()
    assert _ended(db, hold_id) == {"ended": "closed"}
    assert _card(db, hold_id, artifact_id) == "closed"
    assert _asker_inputs(db) == []


# Reviewer item 3b: a stale answer on an ended ask reports that it ended.
def test_answering_an_ended_ask_is_not_offered_and_reports_that_it_ended(db):
    from doceditor import review_bar
    from registry import attention

    plan_id, node_id, artifact_id, hold_id = _asked(db)
    assert review_bar.round_state(db, artifact_id)["ask_raised"] is True
    _withdraw(db, plan_id, node_id, hold_id)

    assert _in_pile(db, "proposal.md") == set()
    assert review_bar.round_state(db, artifact_id)["ask_raised"] is False
    with pytest.raises(review_bar.AnswerRefused) as refused:
        attention.clear_attention(artifact_id, outcome="answer", verdict="approved", db=db)
    assert refused.value.code == "ask_stale"
    assert "replaced" not in str(refused.value)
    assert "ended" in str(refused.value)


# A parent approval never outlives the agent's withdrawal.
@pytest.mark.parametrize("approved", ["before_the_ask", "while_asked"])
def test_approve_to_user_then_withdraw_leaves_the_pile(db, approved):
    from parent_review.disposition import APPROVE_TO_USER, dispose

    passed = SimpleNamespace(blocking_failure=False)
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    artifact_id = _document(db, "proposal.md")
    if approved == "before_the_ask":
        dispose(artifact_id, APPROVE_TO_USER, review_result=passed, db=db)
        hold_id = _ask(db, plan.id, node.id, artifact_id)
    else:
        hold_id = _ask(db, plan.id, node.id, artifact_id)
        dispose(artifact_id, APPROVE_TO_USER, review_result=passed, db=db)
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES

    _withdraw(db, plan.id, node.id, hold_id)

    assert _in_pile(db, "proposal.md") == set()
    assert _needs_you(db) == []


# Rows written before asks owned their state leave the pile with no migration run.
def test_a_legacy_raised_approval_on_an_ended_ask_is_absent_without_migration(db):
    from parent_review.disposition import APPROVE_TO_USER
    from plans import artifact_links

    approved = json.dumps({"attention_state": "raised", "disposition_outcome": APPROVE_TO_USER})
    plan_id, node_id, artifact_id, hold_id = _asked(db)
    _withdraw(db, plan_id, node_id, hold_id)
    # Raised metadata with a parent approval, as the old reconciler left it.
    db.execute("UPDATE artifact_identities SET metadata=? WHERE id=?", (approved, artifact_id))
    # A phantom: an ask link from a task that never held a hold.
    phantom = service.add_node(
        plan_id, kind="task", title="Old ask", acceptance_criteria="Reviewed", db=db,
    )
    legacy_id = _document(db, "legacy.md")
    artifact_links.link(
        phantom.id, legacy_id, "attention", actor_id="owner", provenance="test.legacy", db=db,
    )
    db.execute("UPDATE artifact_identities SET metadata=? WHERE id=?", (approved, legacy_id))
    db.commit()

    assert _in_pile(db, "proposal.md") == set()
    assert _in_pile(db, "legacy.md") == set()
    assert _needs_you(db) == []


def test_an_approval_after_the_ask_ended_is_refused(db):
    from doceditor import review_bar
    from parent_review.disposition import APPROVE_TO_USER, DispositionError, dispose

    plan_id, node_id, artifact_id, hold_id = _asked(db)
    _withdraw(db, plan_id, node_id, hold_id)

    with pytest.raises(DispositionError, match=review_bar.STALE):
        dispose(artifact_id, APPROVE_TO_USER, review_result=SimpleNamespace(blocking_failure=False), db=db)
    assert "disposition_outcome" not in _metadata(db, artifact_id)
    assert _in_pile(db, "proposal.md") == set()


# A deferral binds only to the hold it deferred.
def test_defer_expiry_withdraw_and_reask_follow_the_open_hold(db):
    from automation import watches
    from registry import attention

    plan_id, node_id, artifact_id, hold_id = _asked(db)
    deadline = {"type": "deadline", "value": "2026-10-01T16:00:00+00:00"}

    attention.clear_attention(artifact_id, outcome="defer", condition=deadline, db=db)
    [item] = attention.list_raised(db=db)
    assert item["metadata"]["attention_state"] == "deferred"
    assert item["metadata"]["deferred_hold_id"] == hold_id
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES

    watch = watches.arm_watch(artifact_id, deadline, owner_thread="owner", db=db)
    watches.expire_watch(watch.id, db=db)
    [item] = attention.list_raised(db=db)
    assert item["metadata"]["attention_state"] == "raised"

    _withdraw(db, plan_id, node_id, hold_id)
    assert _in_pile(db, "proposal.md") == set()

    second = _ask(db, plan_id, node_id, artifact_id)
    [item] = attention.list_raised(db=db)
    assert item["metadata"]["attention_state"] == "raised"
    assert second != hold_id


def test_a_stale_deferral_stamp_does_not_defer_a_new_ask(db):
    from registry import attention

    plan_id, node_id, artifact_id, hold_id = _asked(db)
    attention.clear_attention(
        artifact_id, outcome="defer",
        condition={"type": "deadline", "value": "2026-10-01T16:00:00+00:00"}, db=db,
    )
    _withdraw(db, plan_id, node_id, hold_id)
    assert _in_pile(db, "proposal.md") == set()

    _ask(db, plan_id, node_id, artifact_id)

    [item] = attention.list_raised(db=db)
    assert item["metadata"]["attention_state"] == "raised"
    assert [row["node_id"] for row in _needs_you(db)] == [node_id]


# Each person disposition on an open ask clears the hold, releases the task,
# empties the pile and reaches the asker once.
@pytest.mark.parametrize(("outcome", "ended", "extra"), [
    ("reject_with_reason", "rejected", {}),
    ("request_revision", "revision_requested", {}),
    ("answer_internally", "answered_internally", {}),
    ("supersede", "replaced", {"replacement_id": "replacement.md"}),
])
def test_each_disposition_releases_the_task_with_exactly_one_asker_input(
    db, outcome, ended, extra, monkeypatch,
):
    from parent_review import disposition
    from parent_review.disposition import dispose

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        disposition, "emit_product_event", lambda category, action, **_: events.append((category, action)),
    )
    _plan_id, node_id, artifact_id, hold_id = _asked(db)
    assert admission.project(node_id, db=db)["condition"] == "blocked"
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES
    assert _asker_inputs(db) == []

    dispose(artifact_id, outcome, reason="The person's reason", db=db, **extra)

    assert _ended(db, hold_id)["ended"] == ended
    assert admission.project(node_id, db=db)["condition"] != "blocked"
    assert _in_pile(db, "proposal.md") == set()
    assert _needs_you(db) == []
    [wake] = _asker_inputs(db)
    assert wake.startswith(f"plan-wake:ask_released:{node_id}:")
    content = db.execute("SELECT content FROM messages WHERE id=?", (wake,)).fetchone()["content"]
    assert "The person's reason" in content
    assert events == [("parent_disposition", outcome)]
    assert _metadata(db, artifact_id)["disposition_outcome"] == outcome


def test_a_route_back_reaches_a_document_owner_the_release_did_not_wake(db):
    from parent_review.disposition import REJECT_WITH_REASON, dispose
    from registry.ownership import rehome_artifact

    db.execute(
        """INSERT INTO threads (id,user_id,project_id,title,model,created_at,updated_at)
           VALUES ('writer','user','project','Writer','test/model','t','t')"""
    )
    _plan_id, _node_id, artifact_id, _hold_id = _asked(db)
    # The document moves to another thread while its ask still holds the asker's task.
    rehome_artifact(
        integration="workspace", type="document", external_id="proposal.md",
        new_owner_thread_id="writer", actor_thread_id="owner", authority="test",
        reason="Hand-off", source_path="test.ask_pile", db=db,
    )

    result = dispose(artifact_id, REJECT_WITH_REASON, reason="Not yet", db=db)

    assert result.routed_to == "writer"
    assert result.route_disposition is not None
    assert db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE change_kind='artifact.disposition'",
    ).fetchone()[0] == 1


def test_an_approval_on_an_open_ask_records_its_audit_but_not_its_state(db):
    from parent_review.disposition import APPROVE_TO_USER, dispose

    _plan_id, _node_id, artifact_id, _hold_id = _asked(db)

    dispose(artifact_id, APPROVE_TO_USER, review_result=SimpleNamespace(blocking_failure=False), db=db)

    metadata = _metadata(db, artifact_id)
    assert metadata["disposition_outcome"] == APPROVE_TO_USER
    assert metadata["disposition_audit"] == {"blocking_failure": False}
    assert "attention_state" not in metadata


def _metadata(db, artifact_id: str) -> dict:
    return json.loads(db.execute(
        "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
    ).fetchone()["metadata"] or "{}")


# Retirement ends the ask whatever the metadata reads.
def test_retiring_an_asked_artifact_ends_its_ask_as_retired(db):
    from registry import models

    _plan_id, node_id, artifact_id, hold_id = _asked(db)
    assert json.loads(db.execute(
        "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
    ).fetchone()["metadata"] or "{}").get("attention_state") != "raised"
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES

    models.retire(artifact_id, db=db)

    assert _ended(db, hold_id) == {"ended": "retired"}
    assert admission.project(node_id, db=db)["condition"] != "blocked"
    assert _in_pile(db, "proposal.md") == set()


# Reviewer item 3c: ``ended`` is written by plans.ask_endings alone.
def test_ended_is_written_only_by_the_one_ask_ending_writer():
    backend = Path(__file__).resolve().parents[1]
    writers = []
    for path in sorted(backend.rglob("*.py")):
        relative = path.relative_to(backend).as_posix()
        if relative.startswith(("tests/", "venv/", ".venv/")) or "/tests/" in relative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            keys = []
            if isinstance(node, ast.Dict):
                keys = [key for key in node.keys if isinstance(key, ast.Constant)]
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                keys = [node.slice] if isinstance(node.slice, ast.Constant) else []
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
                keys = [ast.Constant(kw.arg) for kw in node.keywords]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "$.ended" in node.value:
                    writers.append(f"{relative}:{node.lineno}")
                continue
            if any(key.value == "ended" for key in keys):
                writers.append(f"{relative}:{node.lineno}")
    assert [writer.split(":")[0] for writer in writers] == ["plans/ask_endings.py"], writers


def test_open_requests_are_defined_once():
    """The bar, the answer, the rule, the pile and the plan's link state read one query.

    An open review request is a user hold owned by the artifact its task links
    for attention; only ``user_ask.open_asks_sql`` writes that join.
    """
    backend = Path(__file__).resolve().parents[1]
    join = re.compile(r"h\.owner_id\s*=\s*l\.artifact_id|l\.artifact_id\s*=\s*h\.owner_id")
    sites = [
        f"{path.relative_to(backend).as_posix()}:{number}"
        for path in sorted(backend.rglob("*.py"))
        if not path.relative_to(backend).as_posix().startswith(("tests/", "venv/", ".venv/"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if join.search(line)
    ]
    assert [site.split(":")[0] for site in sites] == ["plans/user_ask.py"], sites


# The pile refetches on ``plans_changed``, so placing an ask,
# the agent withdrawing it and the person answering it in the pile each nudge
# the person after commit.
def test_ask_place_withdraw_and_pile_answer_each_nudge_the_person(db, monkeypatch):
    from plans import notifications, nudge_scheduling
    from registry import attention

    scheduled: list[tuple[str, ...]] = []
    monkeypatch.setattr(nudge_scheduling, "_schedule_delivery", scheduled.append)

    def nudged_users(messages) -> dict[str, set[str]]:
        users: dict[str, set[str]] = {}
        for user_id, message in messages:
            assert message["type"] == "plans_changed"
            users.setdefault(user_id, set()).update(row["plan_id"] for row in message["plans"])
        return users

    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    artifact_id = _document(db, "proposal.md")
    ask = {
        "node_id": node.id, "reason_key": "review", "reason": "Review it",
        "owner_kind": "user", "ask_artifact_id": artifact_id,
    }

    placed = _plan_op(db, plan.id, "block_node", ask)
    assert nudged_users(placed.messages) == {"user": {plan.id}}
    hold_id = str(db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchone()["id"])

    withdrawn = _plan_op(db, plan.id, "resolve_blocker", {"node_id": node.id, "hold_id": hold_id})
    assert nudged_users(withdrawn.messages) == {"user": {plan.id}}

    _plan_op(db, plan.id, "block_node", ask)
    scheduled.clear()
    attention.clear_attention(artifact_id, outcome="answer", verdict="approved", db=db)
    assert [set(ids) for ids in scheduled] == [{plan.id}]
    assert nudged_users(notifications.prepare_after_commit(scheduled[0], db=db)) == {
        "user": {plan.id},
    }


# Two open asks on one document, left from before the one-open-ask rule, show
# once in the pile and once per asking task in needs-you.
def test_legacy_duplicate_asks_show_once_in_the_pile_and_once_per_task(db):
    from api.attention import _summarize_raised
    from plans import holds, user_ask
    from registry import attention

    plan_id, first, artifact_id, _hold = _asked(db)
    second = service.add_node(
        plan_id, kind="task", title="Second review", acceptance_criteria="Reviewed", db=db,
    )
    # Seeded below the rule (``holds.block_hold``): such duplicates predate it.
    holds.place_hold(
        second.id, kind="blocked", reason_key="review", reason="Review it", owner_kind="user",
        owner_id=artifact_id, expected_revision=service._node_row(db, second.id)["revision"],
        actor_id="owner", provenance="test.legacy", db=db,
    )
    user_ask.attach(db, second.id, artifact_id, actor_id="owner", provenance="test.legacy")
    db.commit()

    assert [row["external_id"] for row in attention.list_raised(db=db)] == ["proposal.md"]
    assert [row["external_id"] for row in _summarize_raised(None, "user", db=db)] == ["proposal.md"]
    assert _in_pile(db, "proposal.md") == OPEN_ASK_ROUTES
    assert sorted(item["node_id"] for item in _needs_you(db)) == sorted([first, second.id])
