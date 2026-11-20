"""delegate_stop: a parent stops its own workers, through archive-then-stop."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from delegation_fixtures import deleg_db  # noqa: F401

MODEL = "claude-code/sonnet"


def _thread(db, sid, spawned_by=None, *, standing=0):
    db.execute(
        "INSERT INTO threads (id,user_id,model,spawned_by,agent_enabled,title,created_at,updated_at) "
        "VALUES (?,'u-test',?,?,?,?,'t','t')", (sid, MODEL, spawned_by, standing, sid))
    db.commit()


def _run_turn(db, sid):
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?,?,0,?,'running','2026-06-11T00:00:00','test')", (f"turn-{sid}", sid, MODEL))
    db.commit()


@pytest.fixture
def tree(deleg_db, monkeypatch):
    """root (standing) -> parent -> worker -> grand; root -> sibling; plus an unrelated root."""
    _thread(deleg_db, "root", standing=1)
    _thread(deleg_db, "parent", "root")
    _thread(deleg_db, "worker", "parent")
    _thread(deleg_db, "grand", "worker")
    _thread(deleg_db, "sibling", "parent")
    _thread(deleg_db, "outsider")
    stops, woken = [], []

    async def fake_stop(sid, turn_id, *, plan_limit=True):
        stops.append((sid, turn_id, plan_limit, deleg_db.execute(
            "SELECT lifecycle_state FROM threads WHERE id=?", (sid,)).fetchone()[0]))
        return SimpleNamespace(outcome="stopped")

    monkeypatch.setattr("ws.stop_control.stop_turn_for_cause", fake_stop)
    monkeypatch.setattr("plans.public.on_child_thread_archived",
                        lambda conn, sid, *, archived_at: woken.append(sid))
    return SimpleNamespace(db=deleg_db, stops=stops, woken=woken)


async def _stop(caller, target):
    from tools.delegation_stop import DelegateStopTool

    return await DelegateStopTool().execute(target, _thread_id=caller)


def _state(db, sid):
    return tuple(db.execute("SELECT lifecycle_state, archived_reason FROM threads WHERE id=?", (sid,)).fetchone())


@pytest.mark.asyncio
async def test_parent_stops_worker_and_subtree(tree):
    _run_turn(tree.db, "worker")
    _run_turn(tree.db, "grand")
    out = await _stop("parent", "worker")
    assert "worker: stopped mid-turn" in out and "grand: stopped mid-turn" in out
    assert _state(tree.db, "worker") == ("archived", "stopped_by_delegator")
    assert _state(tree.db, "grand") == ("archived", "stopped_by_delegator")
    assert _state(tree.db, "sibling")[0] == "active"
    assert tree.woken == []  # the direct parent called: it is not woken


@pytest.mark.asyncio
async def test_archive_happens_before_the_turn_stop(tree):
    _run_turn(tree.db, "worker")
    await _stop("parent", "worker")
    assert tree.stops == [("worker", "turn-worker", False, "archived")]


@pytest.mark.asyncio
async def test_grandparent_ok_and_wakes_direct_parent(tree):
    out = await _stop("root", "worker")
    assert "worker: was idle" in out
    assert tree.woken == ["worker"]
    assert "parent (its spawner) was told" in out


@pytest.mark.asyncio
async def test_corsia_agent_ok(tree):
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator

    _thread(tree.db, "corsia")
    set_scope_coordinator(WORKSPACE_SCOPE, "corsia")
    out = await _stop("corsia", "worker")
    assert _state(tree.db, "worker")[0] == "archived" and "Error" not in out
    assert tree.woken == ["worker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,target", [
    ("sibling", "worker"), ("worker", "parent"), ("outsider", "worker"), ("worker", "worker")])
async def test_everyone_else_is_refused(tree, caller, target):
    out = await _stop(caller, target)
    assert out.startswith("Error:")
    assert _state(tree.db, target)[0] == "active"


@pytest.mark.asyncio
async def test_refusal_names_who_can_stop_it(tree):
    out = await _stop("sibling", "worker")
    assert "parent" in out and "CORSIA agent" in out


@pytest.mark.asyncio
async def test_standing_agent_and_rootless_target_refused(tree):
    assert "only the person" in await _stop("parent", "root")
    assert "only the person" in await _stop("outsider", "outsider")


@pytest.mark.asyncio
async def test_standing_agent_beneath_is_skipped_with_its_subtree(tree):
    _thread(tree.db, "standing", "worker", standing=1)
    _thread(tree.db, "under-standing", "standing")
    out = await _stop("parent", "worker")
    assert "standing: skipped" in out
    assert _state(tree.db, "standing")[0] == "active"
    assert _state(tree.db, "under-standing")[0] == "active"
    assert _state(tree.db, "grand")[0] == "archived"


@pytest.mark.asyncio
async def test_idle_worker_with_queued_input_starts_nothing_after_stop(tree):
    from infra.database import write_transaction
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import _claimable_prefix

    accept_thread_input("worker", role="user", content="more", intent="apply_now", message_id="q1", db=tree.db)
    tree.db.commit()
    with write_transaction(tree.db, scope="before"):
        assert _claimable_prefix(tree.db, "worker") is not None
    out = await _stop("parent", "worker")
    with write_transaction(tree.db, scope="after"):
        assert _claimable_prefix(tree.db, "worker") is None
    assert "1 queued input(s) left held" in out


@pytest.mark.asyncio
async def test_unsuccessful_stop_is_reported_not_called_stopped(tree, monkeypatch):
    async def stale(sid, turn_id, *, plan_limit=True):
        return SimpleNamespace(outcome="stale_active")

    monkeypatch.setattr("ws.stop_control.stop_turn_for_cause", stale)
    _run_turn(tree.db, "worker")
    out = await _stop("parent", "worker")
    assert "worker: could not stop (stop outcome: stale_active)" in out


@pytest.mark.asyncio
async def test_wakes_watches_and_schedules_are_cancelled(tree):
    tree.db.execute(
        "INSERT INTO plan_liveness_wakes (id,thread_id,status,deadline,armed_at,updated_at) "
        "VALUES ('w','worker','armed','t','t','t')")
    tree.db.commit()
    await _stop("parent", "worker")
    status = tree.db.execute("SELECT status FROM plan_liveness_wakes WHERE thread_id='worker'").fetchone()[0]
    assert status == "cancelled"


@pytest.mark.asyncio
async def test_reattach_after_stop_reactivates(tree):
    from core.thread_lifecycle import reactivate_settled_worker

    await _stop("parent", "worker")
    assert reactivate_settled_worker(tree.db, "worker") is True
    assert _state(tree.db, "worker") == ("active", None)


@pytest.mark.asyncio
async def test_parent_is_told_which_open_step_to_close(tree, monkeypatch):
    monkeypatch.setattr("plans.public.has_open_structured_attachment", lambda sid, *, db=None: sid == "worker")
    out = await _stop("parent", "worker")
    assert "still open" in out and "close it or re-delegate" in out


@pytest.mark.asyncio
async def test_plain_send_to_a_stopped_worker_is_refused(tree):
    from infra.input_admission import accept_thread_input
    from infra.send_types import SendRejected

    await _stop("parent", "worker")
    with pytest.raises(SendRejected) as refused:
        accept_thread_input("worker", role="user", content="hi", intent="apply_now", message_id="q2", db=tree.db)
    assert refused.value.status_code == 409 and "archived" in refused.value.detail


@pytest.mark.asyncio
async def test_attaching_a_step_reactivates_the_stopped_worker_and_its_held_input_runs(tree):
    from infra.database import write_transaction
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import _claimable_prefix
    from plans import mutations, service

    accept_thread_input("worker", role="user", content="more", intent="apply_now", message_id="q1", db=tree.db)
    tree.db.commit()
    await _stop("parent", "worker")
    node = service.add_node(
        service.ensure_plan("thread", "parent", db=tree.db).id, kind="task", title="Delegate",
        acceptance_criteria="Reviewed", spawn_kind="claude-code", db=tree.db)
    service.transition_node(node.id, "active", db=tree.db)
    mutations.attach_spawned_thread(node.id, "worker", db=tree.db)
    tree.db.commit()
    assert _state(tree.db, "worker") == ("active", None)
    with write_transaction(tree.db, scope="after_attach"):
        assert _claimable_prefix(tree.db, "worker") is not None


def test_explicit_archive_predicates_agree(tree):
    from infra.queries import thread_available_sql, thread_is_explicitly_archived, thread_unavailable_reason

    for reason, hidden in (("user", True), ("stopped_by_delegator", True), ("assignment_settled", False), ("dormancy", False)):
        tree.db.execute("UPDATE threads SET lifecycle_state='archived', archived_reason=? WHERE id='worker'", (reason,))
        row = tree.db.execute("SELECT * FROM threads WHERE id='worker'").fetchone()
        usable = tree.db.execute(f"SELECT 1 FROM threads t WHERE t.id='worker' AND {thread_available_sql('t')}").fetchone()
        assert (thread_unavailable_reason(row) is not None) == hidden == (usable is None) == thread_is_explicitly_archived(row)


@pytest.mark.asyncio
async def test_open_watches_and_schedules_are_retired(tree):
    tree.db.execute(
        "INSERT INTO expected_event_watches (id,owner_thread_id,condition_type,status,armed_at,created_at,updated_at) VALUES ('wt','worker','deadline','armed','t','t','t')")
    tree.db.execute(
        "INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,created_by_actor,created_at,updated_at) VALUES ('sc','worker','s','clock','x','t',1,'active','a','t','t')")
    tree.db.commit()
    await _stop("parent", "worker")
    assert tree.db.execute("SELECT status FROM expected_event_watches WHERE id='wt'").fetchone()[0] == "resolved"
    assert tree.db.execute("SELECT state FROM automation_schedules WHERE id='sc'").fetchone()[0] == "cancelled"
