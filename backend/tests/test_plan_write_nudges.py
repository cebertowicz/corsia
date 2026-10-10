"""Every plan write names the plans it touched in one post-commit nudge.

The chat list and the plan pane refresh only on a nudge, so a write nobody
announces (a deadline passing, a watch resolved by an event, a dependent in
another plan) would leave them stale until a reconnect.
"""

from __future__ import annotations

import asyncio

import pytest

import infra.app_loop as app_loop
import infra.state as state
from automation import watches
from infra.database import get_db
from plans import hierarchy, notifications, nudge_scheduling, plan_write, service
from tests.registry_helpers import make_session, seed_artifact

PAST = {"type": "deadline", "value": "2000-01-01T00:00:00+00:00"}


class Sent:
    """Messages delivered to sockets, once the writer's hand-off to the loop has run."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, list[str]]] = []
        self.scheduled: list = []

    def flush(self) -> list[tuple[str, list[str]]]:
        """Run what writers handed the application loop; return and clear the deliveries."""
        while self.scheduled:
            asyncio.run(self.scheduled.pop(0))
        out, self.messages = self.messages, []
        return out


@pytest.fixture
def sent(monkeypatch) -> Sent:
    captured = Sent()
    monkeypatch.setattr(
        state, "broadcast_to_user",
        lambda user_id, message: captured.messages.append(
            (user_id, [change["plan_id"] for change in message["plans"]])
        ),
    )
    monkeypatch.setattr(app_loop, "submit", captured.scheduled.append)
    return captured


def _plan(db):
    owner = make_session(db)
    plan = service.ensure_plan("thread", owner, db=db)
    db.commit()
    return owner, plan


def _add_step(db, plan_id: str, title: str = "Step"):
    return service.add_node(plan_id, kind="task", title=title, acceptance_criteria="Done", db=db)


def test_a_plan_write_nudges_the_plan_it_touched_once_it_commits(isolated_db, sent):
    db = get_db()
    _owner, plan = _plan(db)
    sent.flush()

    _add_step(db, plan.id)

    assert sent.flush() == [("steve", [plan.id])]


def test_a_rolled_back_plan_write_nudges_nothing(isolated_db, sent):
    db = get_db()
    _owner, plan = _plan(db)
    sent.flush()

    with pytest.raises(RuntimeError):
        with plan_write.transaction(db):
            _add_step(db, plan.id)
            raise RuntimeError("abandon")

    assert sent.scheduled == []
    assert sent.flush() == []


def test_a_caller_that_delivers_its_own_nudge_collects_the_ids_instead(isolated_db, sent):
    db = get_db()
    _owner, plan = _plan(db)
    sent.flush()

    with notifications.collect_touched() as touched:
        _add_step(db, plan.id)

    assert touched == [plan.id]
    assert sent.scheduled == []
    assert sent.flush() == []


def test_a_watch_deadline_passing_nudges_its_plan(isolated_db, sent):
    db = get_db()
    owner, plan = _plan(db)
    node = _add_step(db, plan.id, "Wait")
    seed_artifact(
        db, artifact_id="watched", integration="test", type="note",
        external_id="watched-item", parent_thread_id=owner,
    )
    db.commit()
    watch = watches.arm_watch(
        "watched", PAST, owner_thread=owner, plan_id=plan.id, plan_node_id=node.id, db=db,
    )
    db.commit()
    sent.flush()

    assert watches.sweep_expired("2026-10-04T00:00:00+00:00") == 1

    assert watches.get_watch(watch.id).status == "expired"
    assert sent.flush() == [("steve", [plan.id])]


def test_a_host_access_nudge_names_the_leases_root_and_its_visible_thread(isolated_db, monkeypatch):
    from host_access import supervisor

    db = get_db()
    visible = make_session(db)
    child = make_session(db)
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (visible, child))
    db.commit()
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(state, "broadcast_to_user", lambda user_id, message: seen.append((user_id, message)))

    supervisor._notify_changed({"id": "lease-1", "user_id": "steve", "root_thread_id": child})

    assert seen == [(
        "steve",
        {"type": "host_access_changed", "lease_id": "lease-1", "thread_ids": [child, visible]},
    )]


def _parent_and_child(db):
    """A parent plan and the plan of the worker one of its steps attached."""
    parent_owner, parent = _plan(db)
    child_owner = make_session(db)
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (parent_owner, child_owner))
    child = service.ensure_plan("thread", child_owner, db=db)
    db.execute("UPDATE plans SET parent_plan_id=? WHERE id=?", (parent.id, child.id))
    db.commit()
    return parent, child_owner, child


def test_a_child_plan_write_also_nudges_the_plan_that_attaches_it(isolated_db, sent):
    """A collapsed parent row embeds the child's progress, so it must refresh too."""
    db = get_db()
    parent, _child_owner, child = _parent_and_child(db)
    sent.flush()

    _add_step(db, child.id)

    assert sent.flush() == [("steve", [child.id, parent.id])]


def test_the_attaching_walk_climbs_every_level_and_stops_at_a_cycle(isolated_db):
    db = get_db()
    parent, _child_owner, child = _parent_and_child(db)
    grand_owner = make_session(db)
    grand = service.ensure_plan("thread", grand_owner, db=db)
    db.execute("UPDATE plans SET parent_plan_id=? WHERE id=?", (child.id, grand.id))
    db.commit()

    assert hierarchy.with_attaching_plans([grand.id], db=db) == (grand.id, child.id, parent.id)

    db.execute("UPDATE plans SET parent_plan_id=? WHERE id=?", (grand.id, parent.id))
    db.commit()

    assert hierarchy.with_attaching_plans([grand.id], db=db) == (grand.id, child.id, parent.id)


def test_the_attaching_walk_is_depth_bounded(isolated_db, monkeypatch):
    db = get_db()
    parent, _child_owner, child = _parent_and_child(db)
    monkeypatch.setattr(hierarchy, "_MAX_ANCESTOR_DEPTH", 0)

    assert hierarchy.with_attaching_plans([child.id], db=db) == (child.id,)


@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled"])
def test_a_child_turn_ending_nudges_the_child_and_the_parent_plan(isolated_db, sent, outcome):
    """Turn end flips the parent row (stalled, idle) without any plan write."""
    from automation import turn_end_effects

    db = get_db()
    parent, child_owner, child = _parent_and_child(db)
    sent.flush()

    effects = turn_end_effects._record_turn_end(
        lambda _thread_id, **_kwargs: None, child_owner, {"outcome": outcome},
    )
    turn_end_effects.deliver_turn_end_invalidations(effects)

    assert sent.flush() == [("steve", [child.id, parent.id])]


def test_a_turn_starting_on_an_already_selected_attempt_nudges_the_parent_plan(isolated_db, sent):
    from infra.database import write_transaction

    db = get_db()
    parent, child_owner, child = _parent_and_child(db)
    sent.flush()

    with write_transaction(db, scope="turn_start_test"):
        nudge_scheduling.schedule_thread_after_commit(child_owner, db=db)

    assert sent.flush() == [("steve", [child.id, parent.id])]


def test_a_commit_sends_one_message_per_plan_however_many_writes_named_it(isolated_db, sent):
    from infra.database import write_transaction

    db = get_db()
    _owner, plan = _plan(db)
    sent.flush()

    with write_transaction(db, scope="merge_nudges_test"):
        nudge_scheduling.schedule_after_commit([plan.id], db=db)
        nudge_scheduling.schedule_after_commit([plan.id], db=db)

    assert sent.flush() == [("steve", [plan.id])]


def test_a_nudge_skipped_under_a_foreign_transaction_is_logged_as_a_warning(isolated_db, caplog):
    db = get_db()
    _owner, plan = _plan(db)
    db.execute("BEGIN")
    db.execute("UPDATE plans SET updated_at=updated_at WHERE id=?", (plan.id,))

    with caplog.at_level("WARNING", logger="plans.notifications"):
        nudge_scheduling.schedule_after_commit([plan.id], db=db)

    assert any(record.levelname == "WARNING" for record in caplog.records)
    db.rollback()


def test_restoring_an_archived_child_nudges_its_plan_and_the_parent(isolated_db, sent):
    """Restore changes the child's lifecycle_state, a fact the parent row reads, with no plan write."""
    from core.thread_lifecycle import set_user_archive_state
    from infra.database import write_transaction

    db = get_db()
    parent, child_owner, child = _parent_and_child(db)
    with write_transaction(db, scope="archive_test"):
        set_user_archive_state(db, child_owner, archived=True)
    sent.flush()

    with write_transaction(db, scope="restore_test"):
        assert set_user_archive_state(db, child_owner, archived=False)

    assert sent.flush() == [("steve", [child.id, parent.id])]


def test_a_queued_input_clearing_a_stalled_child_nudges_the_parent_plan(isolated_db, sent):
    from infra.input_admission import accept_thread_input

    db = get_db()
    parent, child_owner, child = _parent_and_child(db)
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('t-err',?,0,'test/model','error','2026-10-04T00:00:00+00:00','boot-1')",
        (child_owner,),
    )
    db.commit()
    sent.flush()

    accept_thread_input(
        child_owner, role="user", content="try again", intent="apply_now", message_id="m-1", db=db,
    )

    assert sent.flush() == [("steve", [child.id, parent.id])]


def test_a_handback_queued_behind_a_running_child_turn_nudges_the_parent_plan(isolated_db, sent):
    """The parent row reads the child's queued input, so it changes before that Turn ends."""
    from infra.input_admission import accept_thread_input

    db = get_db()
    parent, child_owner, child = _parent_and_child(db)
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('t-run',?,0,'test/model','running','2026-10-04T00:00:00+00:00','boot-1')",
        (child_owner,),
    )
    db.commit()
    sent.flush()

    accept_thread_input(
        child_owner, role="user", content="handback", intent="apply_now", message_id="m-2", db=db,
    )

    assert sent.flush() == [("steve", [child.id, parent.id])]


def test_a_rolled_back_savepoint_does_not_nudge_through_an_outer_frames_effect(isolated_db, sent):
    from infra.database import write_transaction

    db = get_db()
    _owner, plan = _plan(db)
    other, other_plan = _plan(db)
    sent.flush()

    with write_transaction(db, scope="outer"):
        nudge_scheduling.schedule_after_commit([plan.id], db=db)
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope="inner"):
                nudge_scheduling.schedule_after_commit([other_plan.id], db=db)
                raise RuntimeError("abandon inner")

    assert sent.flush() == [("steve", [plan.id])]
