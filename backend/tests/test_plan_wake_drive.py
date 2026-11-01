"""A plan wake written outside a plan mutation starts its idle recipient's Turn.

A wake written inside a ``plan_update`` is driven after that mutation commits
(``notifications.deferred_mutation``). The writes here have no mutation to
carry the drive: the harvest of a returned child, run by the continuation
sweep, and the wakes a worker's Turn end owes its delegating parent. Before the
fix each left its wake ``queued`` in an idle parent's FIFO until a human message
arrived (#1703), so each must now leave the recipient with a started Turn, once,
after the write that made the wake has committed.

The lineage lives in the isolated file database so the real drive machinery
(``start_idle_turn`` through ``run_in_db``) sees what the writers committed.
"""

from __future__ import annotations

from automation import plan_liveness_terminal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from automation import plan_liveness
from automation.turn_end_effects import (
    deliver_turn_end_invalidations,
    record_turn_end,
)
from automation.continuation.runner import sweep
from automation.continuation.sources.harvest import HarvestSource
import infra.thread_execution as thread_execution
from infra.database import get_db
from plans import notifications, service
from subagents import cc_event_result
from tests.child_parent_notice_helpers import NOW, delegated, wakes
from tests.plan_review_helpers import submit_review_evidence


@pytest.fixture
def lineage(isolated_db):
    """One grandparent/parent/worker lineage, committed to the isolated database."""
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (
        ("grandparent", None), ("parent", "grandparent"), ("worker", "parent"),
    ):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'opus','t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    return db


@pytest.fixture
def driven(lineage, monkeypatch):
    """Record each drive: which thread, and what its write had committed by then."""
    calls: list[tuple[str, list[str]]] = []

    async def record(thread_id: str) -> None:
        assert not lineage.in_transaction, "a drive ran inside the write it follows"
        calls.append((thread_id, [row["id"] for row in wakes(lineage, thread_id)]))

    monkeypatch.setattr(thread_execution, "drive_thread", record)
    return calls


def _returned_worker(db):
    """Leave the delegated worker's final step up for review, ready to harvest."""
    step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    return step


async def _sweep(db) -> None:
    sources: list[Any] = [HarvestSource()]
    await sweep(NOW.isoformat(), db=db, sources=sources)


def test_revision_wake_supports_the_production_db_none_call_shape(lineage):
    """The revision wake reaches its worker through the default database handle."""
    from plans import plan_wake

    parent_plan = service.ensure_plan("thread", "parent", db=lineage)
    revision = service.add_node(
        parent_plan.id, kind="task", title="Revise the brief",
        acceptance_criteria="Revision is proven", db=lineage,
    )
    from plans import mutations

    mutations.attach_spawned_thread(revision.id, "worker", db=lineage)
    lineage.commit()

    wake_id = plan_wake.notify_revision(
        None, revision.id, recipient="worker", feedback="Cover the failure path",
        worker_step_id="worker-revision-step",
    )

    assert wake_id is not None
    assert wake_id.startswith("plan-wake:revision_requested:")


async def test_a_harvested_return_drives_its_parent_once_after_commit(lineage, driven):
    step = _returned_worker(lineage)

    await _sweep(lineage)

    returned = [row["id"] for row in wakes(lineage)]
    assert len(returned) == 1
    assert service.get_node(step.id, db=lineage).state == "review_requested"
    assert driven == [("parent", returned)]


async def test_standalone_sweep_delivers_the_harvest_invalidation(
    lineage, driven, monkeypatch,
):
    """The production db=None sweep delivers after its harvest commit."""
    _returned_worker(lineage)
    delivered = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda _user_id, message: delivered.append(message),
    )

    await sweep(NOW.isoformat(), sources=[HarvestSource()])

    assert [message["type"] for message in delivered] == ["plans_changed"]
    assert [thread_id for thread_id, _wakes in driven] == ["parent"]


async def test_a_repeat_sweep_adds_no_second_wake_and_no_second_drive(lineage, driven):
    _returned_worker(lineage)

    await _sweep(lineage)
    await _sweep(lineage)
    await _sweep(lineage)

    assert len(wakes(lineage)) == 1
    assert [thread for thread, _wakes in driven] == ["parent"]


async def test_a_live_worker_returns_and_drives_parent_only_after_its_turn_ends(
    lineage, driven,
):
    """#2107: the return-shaped plan is only final after the worker's Turn ends."""
    from infra.thread_driver import finish_turn

    step, plan, setup = delegated(lineage)
    lineage.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('worker-turn','worker',0,'opus','running',?,'boot')""",
        (NOW.isoformat(),),
    )
    service.transition_node(setup.id, "active", actor_id="worker", db=lineage)
    submit_review_evidence(
        setup.id, db=lineage, evidence="Setup proof", actor_id="worker",
    )
    service.transition_node(
        setup.id, "accepted", actor_id="worker", verification_result="passed",
        verified_by="worker", db=lineage,
    )
    lineage.commit()

    # The setup step leaves a transient all-settled plan, not a return.
    await _sweep(lineage)
    assert service.get_node(step.id, db=lineage).state == "active"
    assert wakes(lineage) == []
    assert driven == []

    final = service.add_node(
        plan.id, kind="task", title="Final result", acceptance_criteria="Proved", db=lineage,
    )
    service.transition_node(final.id, "active", actor_id="worker", db=lineage)
    submit_review_evidence(
        final.id, db=lineage, evidence="Final proof", actor_id="worker",
    )
    finish_turn(
        "worker-turn", outcome="completed", ended_at=NOW.isoformat(),
        successor_turn_id="worker-turn-next", boot_epoch="boot", db=lineage,
    )
    lineage.commit()
    effects = await record_turn_end(
        plan_liveness_terminal.on_thread_turn_complete,
        "worker",
    )
    deliver_turn_end_invalidations(effects)
    await notifications.drive_threads(effects.drives)

    await _sweep(lineage)

    returned = [row["id"] for row in wakes(lineage)]
    assert service.get_node(step.id, db=lineage).state == "review_requested"
    assert driven == [("parent", returned)]
    assert len(returned) == 1
    await _sweep(lineage)
    assert driven == [("parent", returned)]


async def test_an_idle_parent_starts_a_turn_that_claims_the_return(lineage, monkeypatch):
    claimed = AsyncMock(return_value=None)
    monkeypatch.setattr(thread_execution, "drive_claimed_batch", claimed)
    _returned_worker(lineage)

    await _sweep(lineage)
    await _sweep(lineage)

    returned = [row["id"] for row in wakes(lineage)]
    assert len(returned) == 1
    claimed.assert_awaited_once()
    batch = claimed.await_args.args[0]
    assert batch.thread_id == "parent"
    # The digest the wake carried rides ahead of it; the wake is what starts the Turn.
    assert batch.items[-1].message_id == returned[0]
    turns = lineage.execute(
        "SELECT status FROM active_turns WHERE thread_id='parent'"
    ).fetchall()
    assert [turn["status"] for turn in turns] == ["running"]


async def test_a_busy_parent_gets_the_return_at_its_boundary_and_no_second_turn(
    lineage, monkeypatch,
):
    claimed = AsyncMock(return_value=None)
    boundary = AsyncMock(return_value=None)
    monkeypatch.setattr(thread_execution, "drive_claimed_batch", claimed)
    monkeypatch.setattr(thread_execution, "_request_active_boundary", boundary)
    lineage.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-busy','parent',0,'opus','running','t','boot')"""
    )
    lineage.commit()
    _returned_worker(lineage)

    await _sweep(lineage)
    await _sweep(lineage)

    returned = wakes(lineage)
    assert len(returned) == 1
    assert returned[0]["delivery_state"] == "queued"
    claimed.assert_not_awaited()
    boundary.assert_awaited_once_with("parent")
    assert lineage.execute(
        "SELECT COUNT(*) FROM active_turns WHERE thread_id='parent'"
    ).fetchone()[0] == 1


async def test_a_childs_errored_turn_drives_the_parent_it_woke(
    lineage, driven,
):
    """F1: the Claude Code seam ends the child's Turn in error."""
    delegated(lineage)
    ctx = SimpleNamespace(child_id="worker", db=None)

    effects = await cc_event_result._record_plan_error(
        ctx,
        error="quota exhausted",
        turn_id="turn-1",
    )
    deliver_turn_end_invalidations(effects)
    await notifications.drive_threads(effects.drives)

    woken = [row["id"] for row in wakes(lineage)]
    assert len(woken) == 1
    assert driven == [("parent", woken)]


async def test_a_childs_empty_plan_turn_end_drives_the_parent_it_woke(
    lineage, driven,
):
    """A11: the Turn ended with nothing planned, so no return is coming."""
    delegated(lineage, with_worker_node=False)
    ctx = SimpleNamespace(child_id="worker", db=None)

    effects = await cc_event_result._record_plan_completion(ctx)
    repeated = await cc_event_result._record_plan_completion(ctx)
    for effect in (effects, repeated):
        deliver_turn_end_invalidations(effect)
        await notifications.drive_threads(effect.drives)

    woken = [row["id"] for row in wakes(lineage)]
    assert len(woken) == 1
    assert driven == [("parent", woken)]


def _kinds(woken: list[str]) -> list[str]:
    """Return each wake's kind, the second part of ``plan-wake:{kind}:...``."""
    return [wake.split(":")[1] for wake in woken]


@pytest.mark.parametrize(
    ("step_type", "payload", "planned", "kind"),
    [
        ("error", {"message": "Not logged in"}, True, "child_turn_errored"),
        ("turn_complete", {}, False, "child_returned_nothing"),
    ],
)
async def test_a_native_childs_turn_end_drives_the_parent_it_woke(
    lineage, driven, step_type, payload, planned, kind,
):
    """The native seam (``StepHandler``) reaches the same drive."""
    from core import step_handler as step_module
    from core.turn_core import TurnIdentity

    delegated(lineage, with_worker_node=planned)
    handler = step_module.StepHandler(
        "worker", {"model": "test/model", "model_name": "Test"},
        AsyncMock(), "turn-worker",
    )
    handler.bind_turn_identity(TurnIdentity("turn-worker", 1))

    await handler.handle(step_type, payload)

    woken = [row["id"] for row in wakes(lineage)]
    assert _kinds(woken) == [kind]
    assert driven == [("parent", woken)]


@pytest.mark.parametrize(
    ("event", "planned", "kind"),
    [
        ({"kind": "turn_failed", "cause": "process_exited"}, True, "child_turn_errored"),
        ({"kind": "result", "is_error": False}, False, "child_returned_nothing"),
    ],
)
def test_a_codex_childs_turn_end_drives_the_parent_it_woke(
    lineage, driven, event, planned, kind,
):
    """The Codex seam reaches the same drive, for a failed and a finished Turn."""
    from codex_runtime_fixtures import _reset_runtime, _run
    from subagents import codex_runtime
    from tests.test_external_harness_plan_liveness import (
        _bind_active_turn,
        _deliver_codex_terminal,
    )

    _reset_runtime()
    delegated(lineage, with_worker_node=planned)
    live = codex_runtime._LiveCodex(
        codex_thread_id="codex-thread", session=None,
        parent_thread_id="parent", name="codex",
    )
    # A delegated worker's input is its parent's brief, never a person's message.
    _bind_active_turn(live, "worker", metadata={"sender_kind": "agent", "from_thread": "parent"})

    _run(_deliver_codex_terminal(
        codex_runtime._make_on_event(live, "worker", MagicMock()), event, live,
    ))

    woken = [row["id"] for row in wakes(lineage)]
    assert _kinds(woken) == [kind]
    assert driven == [("parent", woken)]


async def test_archiving_a_delegated_child_drives_the_parent_it_woke(lineage, driven):
    """The thread-lifecycle seam: a user archive tells the parent its child is gone."""
    from api.thread_mutations import _patch_metadata

    delegated(lineage)

    await _patch_metadata("worker", "user-1", {"archived": True})

    woken = [row["id"] for row in wakes(lineage)]
    assert _kinds(woken) == ["child_thread_archived"]
    assert driven == [("parent", woken)]


async def test_deleting_a_delegated_child_drives_the_parent_it_woke(
    lineage, driven, monkeypatch,
):
    from api import thread_mutations

    delegated(lineage)
    monkeypatch.setattr(thread_mutations, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(
        thread_mutations.runtime_boundary, "quiesce_thread_runtimes", AsyncMock(),
    )

    await thread_mutations.delete_thread("worker", "token")

    woken = [row["id"] for row in wakes(lineage)]
    assert _kinds(woken) == ["child_thread_ended"]
    assert driven == [("parent", woken)]
