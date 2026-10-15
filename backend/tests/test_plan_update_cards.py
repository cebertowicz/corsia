"""Plan outcomes become human-visible thread update cards."""

from __future__ import annotations

import pytest

from infra import thread_updates
from plans import holds, service, update_cards
from plans.boundary import mutate
from tests.plan_ask_helpers import with_actionable_user_ask
from tests.test_attention_queue import _mid
from tests.test_plan_archival import OWNER, _plan, _row, _task, db  # noqa: F401


def _cards(db, thread_id: str = "root") -> list[dict]:
    return thread_updates.read_newest_cards(db, thread_id, min_seq=0)[0]


def _close(db, plan_id: str, node_id: str) -> None:
    mutate(
        plan_id,
        "close_node",
        {
            "node_id": node_id,
            "reason": "No longer needed",
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )


def _review_then_accept(db, node_id: str) -> None:
    service.transition_node(node_id, "active", db=db)
    plan_id = str(_row(db, node_id)["plan_id"])
    mutate(
        plan_id,
        "request_review",
        {"node_id": node_id, "evidence": "Verified", "expected_revision": _row(db, node_id)["revision"]},
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )
    mutate(
        plan_id,
        "accept",
        {"node_id": node_id, "verification_result": "passed", "expected_revision": _row(db, node_id)["revision"]},
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )


def _review_then_accept_followup(db, node_id: str) -> None:
    service.transition_node(node_id, "active", db=db)
    plan_id = str(_row(db, node_id)["plan_id"])
    mutate(
        plan_id,
        "request_review",
        {"node_id": node_id, "evidence": "Verified", "expected_revision": _row(db, node_id)["revision"]},
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )
    mutate(
        plan_id,
        "accept_with_follow_up",
        {
            "node_id": node_id,
            "verification_result": "passed",
            "expected_revision": _row(db, node_id)["revision"],
            "title": "Follow up",
            "acceptance_criteria": "Follow up DOD",
        },
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )


def _block(db, plan_id: str, node_id: str, *, reason_key: str = "needs_user") -> str:
    params = with_actionable_user_ask(
        db,
        plan_id,
        "block_node",
        {
            "node_id": node_id,
            "expected_revision": _row(db, node_id)["revision"],
            "owner_kind": "user",
            "reason": "A user decision is required",
            "reason_key": reason_key,
        },
        OWNER,
    )
    mutate(
        plan_id,
        "block_node",
        params,
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )
    return str(
        db.execute(
            "SELECT id FROM plan_node_holds WHERE node_id=? ORDER BY rowid DESC LIMIT 1",
            (node_id,),
        ).fetchone()["id"]
    )


@pytest.mark.parametrize(
    ("outcome", "expected_kind"),
    [
        ("accepted", "done"),
        ("accepted_with_followup", "done_follow_up"),
        ("failed", "failed"),
        ("closed", "closed"),
    ],
)
def test_terminal_top_level_outcomes_write_cards(db, outcome, expected_kind):
    plan = _plan(db)
    node = _task(db, plan.id, title=f"Outcome {outcome}")

    if outcome == "accepted":
        _review_then_accept(db, node.id)
    elif outcome == "accepted_with_followup":
        _review_then_accept_followup(db, node.id)
    elif outcome == "failed":
        service.transition_node(node.id, "failed", db=db)
    else:
        _close(db, plan.id, node.id)

    assert [(card["kind"], card["node_id"], card["title"]) for card in _cards(db)] == [
        (expected_kind, node.id, f"Outcome {outcome}"),
    ]


def test_a_blocked_user_wait_writes_a_card_and_snapshots_the_event_title(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Ask for sign-off")

    _block(db, plan.id, node.id)
    db.execute("UPDATE plan_nodes SET title='Renamed later' WHERE id=?", (node.id,))

    assert [(card["kind"], card["title"]) for card in _cards(db)] == [
        ("blocked", "Ask for sign-off"),
    ]


def test_nested_nodes_are_quiet_unless_they_are_assigned_to_the_user(db):
    plan = _plan(db)
    parent = service.add_node(
        plan.id, kind="phase", title="Parent", acceptance_criteria="Parent DOD", db=db,
    )
    db.commit()
    nested = _task(db, plan.id, title="Nested", parent_id=parent.id)
    user_nested = _task(
        db, plan.id, title="User nested", parent_id=parent.id, assignee="user",
    )

    service.transition_node(nested.id, "failed", db=db)
    service.transition_node(user_nested.id, "failed", db=db)

    assert [(card["kind"], card["node_id"]) for card in _cards(db)] == [
        ("failed", user_nested.id),
    ]


def test_a_nested_node_blocked_on_a_user_ask_writes_a_card(db):
    plan = _plan(db)
    parent = service.add_node(
        plan.id, kind="phase", title="Parent", acceptance_criteria="Parent DOD", db=db,
    )
    db.commit()
    nested = _task(db, plan.id, title="Review the draft", parent_id=parent.id)

    _block(db, plan.id, nested.id)

    assert [(card["kind"], card["node_id"], card["title"]) for card in _cards(db)] == [
        ("blocked", nested.id, "Review the draft"),
    ]


@pytest.mark.parametrize("owner", ["child", "root"], ids=["hidden-subagent", "archived-thread"])
def test_hidden_or_archived_thread_owners_are_excluded(db, owner):
    plan = _plan(db, owner)
    node = _task(db, plan.id, title="Hidden outcome")
    if owner == "root":
        db.execute("UPDATE threads SET archived_at='t' WHERE id='root'")

    service.transition_node(node.id, "failed", db=db)

    assert _cards(db, owner) == []


def test_non_thread_plan_owners_are_excluded(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Scope-owned task")
    db.execute("UPDATE plans SET owner_kind='project_scope' WHERE id=?", (plan.id,))

    service.transition_node(node.id, "failed", db=db)

    assert db.execute("SELECT COUNT(*) FROM thread_updates").fetchone()[0] == 0


def test_archive_clear_and_pause_events_write_nothing(db):
    plan = _plan(db)
    archived = _task(db, plan.id, title="Archived")
    paused = _task(db, plan.id, title="Paused")
    blocked = _task(db, plan.id, title="Blocked then cleared")

    mutate(
        plan.id,
        "archive_node",
        {"node_id": archived.id, "expected_revision": _row(db, archived.id)["revision"]},
        actor=OWNER,
        provenance="test.plan_update_cards",
        mutation_id=_mid(),
        db=db,
    )
    pause = holds.place_hold(
        paused.id,
        kind="paused",
        reason_key="maintenance",
        reason="Wait for maintenance",
        owner_kind="thread",
        owner_id="root",
        expected_revision=_row(db, paused.id)["revision"],
        actor_id="root",
        provenance="test.plan_update_cards",
        db=db,
    )
    hold_id = _block(db, plan.id, blocked.id)
    holds.clear(
        blocked.id,
        hold_id,
        expected_kind="blocked",
        expected_revision=_row(db, blocked.id)["revision"],
        actor_id="root",
        provenance="test.plan_update_cards",
        db=db,
    )

    assert pause is not None
    assert [(card["kind"], card["node_id"]) for card in _cards(db)] == [
        ("blocked", blocked.id),
    ]


def test_an_open_matching_block_is_suppressed_but_a_cleared_one_is_new_news(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Await decision")

    first = _block(db, plan.id, node.id, reason_key="decision")
    holds.clear(
        node.id,
        first,
        expected_kind="blocked",
        expected_revision=_row(db, node.id)["revision"],
        actor_id="root",
        provenance="test.plan_update_cards",
        db=db,
    )
    _block(db, plan.id, node.id, reason_key="decision")
    _block(db, plan.id, node.id, reason_key="decision")

    assert [card["kind"] for card in _cards(db)] == ["blocked", "blocked"]


def test_a_different_open_block_reason_is_new_news(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Await decision")

    _block(db, plan.id, node.id, reason_key="approval")
    _block(db, plan.id, node.id, reason_key="information")

    assert [card["kind"] for card in _cards(db)] == ["blocked", "blocked"]


def test_replaying_an_outcome_event_id_is_a_no_op(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Replay once")
    event_id = "same-event"

    update_cards.observe_node_event(
        db,
        node_id=node.id,
        event="state_transition",
        event_id=event_id,
        new_state="failed",
        payload=None,
        created_at="2026-10-06T19:00:00",
    )
    update_cards.observe_node_event(
        db,
        node_id=node.id,
        event="state_transition",
        event_id=event_id,
        new_state="failed",
        payload=None,
        created_at="2026-10-06T19:00:00",
    )

    assert [(card["kind"], card["node_id"]) for card in _cards(db)] == [
        ("failed", node.id),
    ]


def test_a_writer_failure_rolls_back_the_plan_mutation(db, monkeypatch):
    plan = _plan(db)
    node = _task(db, plan.id, title="Rollback")

    def fail_writer(*_args, **_kwargs):
        raise RuntimeError("update store unavailable")

    monkeypatch.setattr(update_cards, "record_plan_update", fail_writer)

    with pytest.raises(RuntimeError, match="update store unavailable"):
        service.transition_node(node.id, "failed", db=db)

    assert _row(db, node.id)["state"] == "pending"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='state_transition'",
        (node.id,),
    ).fetchone()[0] == 0
    assert _cards(db) == []


def test_block_suppression_reads_only_id_and_reason_key(db):
    plan = _plan(db)
    node = _task(db, plan.id, title="Private blocker")
    statements: list[str] = []

    db.set_trace_callback(statements.append)
    try:
        _block(db, plan.id, node.id, reason_key="keep-private")
    finally:
        db.set_trace_callback(None)

    suppression_reads = [
        statement for statement in statements
        if "SELECT id FROM plan_node_holds" in statement
        and "reason_key" in statement
        and "cleared_at IS NULL" in statement
    ]
    assert len(suppression_reads) == 1
    assert " reason," not in suppression_reads[0]
    assert "evidence" not in suppression_reads[0]
    assert "feedback" not in suppression_reads[0]
