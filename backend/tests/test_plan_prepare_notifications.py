"""P5 post-reset tests for plan harvest transaction and loop ownership."""

from __future__ import annotations

from collections.abc import Iterator

import asyncio
import sqlite3

import pytest

from automation.continuation import runner, triggers
from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
import plans.delegation as delegation
import plans.mutations as mutations
import plans.notifications as notifications
import plans.service as service
from tests.plan_review_helpers import submit_review_evidence

NOW = "2026-08-01T00:00:00+00:00"


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class _EarlySource:
    """Stand in for any source that enqueues before plan material discovery."""

    kind = "t2_watch_expiry"

    def due(self, _now: str, *, db) -> list[triggers.TriggerRequest]:
        return [
            triggers.TriggerRequest(
                kind=self.kind,
                thread_id="parent-a",
                scope_id="project-1",
                subject_id="watch-1",
                transition="idle:-",
                reason="watch_expired",
                details={},
            )
        ]

    def settle(self, _trigger, _outcome, _reason, *, db) -> None:
        return None


class _FailCommit:
    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self) -> None:
        raise RuntimeError("commit failed")


@pytest.fixture
def db(monkeypatch) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for project_id in ("project-1", "project-2"):
        connection.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES (?,?, 't','t')",
            (project_id, project_id),
        )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def ws_registry():
    from infra.state import ws as ws_state

    previous = dict(ws_state._ws_user_clients)
    ws_state._ws_user_clients.clear()
    try:
        yield ws_state
    finally:
        ws_state._ws_user_clients.clear()
        ws_state._ws_user_clients.update(previous)


def _session(db, thread_id, user_id, project_id, spawned_by=None) -> None:
    db.execute(
        """INSERT OR IGNORE INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES (?,?,?,?,'test/model','t','t')""",
        (thread_id, user_id, project_id, spawned_by),
    )


def _accept(node_id: str, *, db) -> None:
    service.transition_node(node_id, "active", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id,
        "accepted",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )


def _seed_attachment(
    db,
    parent_id: str,
    child_id: str,
    *,
    user_id: str = "user-1",
    project_id: str = "project-1",
    terminal: bool = True,
):
    _session(db, parent_id, user_id, project_id)
    _session(db, child_id, user_id, project_id, spawned_by=parent_id)
    db.commit()
    parent = service.ensure_plan("thread", parent_id, db=db)
    parent_node = service.add_node(
        parent.id,
        kind="task",
        title=f"Delegate {child_id}",
        acceptance_criteria="Delegated result is reviewed",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(parent_node.id, "active", db=db)
    child = service.ensure_plan("thread", child_id, db=db)
    child_node = service.add_node(
        child.id,
        kind="task",
        title=f"Result {child_id}",
        acceptance_criteria="Child result passes",
        db=db,
    )
    mutations.attach_spawned_thread(parent_node.id, child_id, db=db)
    if terminal:
        _accept(child_node.id, db=db)
    return parent.id, parent_node.id


def _routed(db) -> int:
    """Triggers raised by anything other than the test's own early source."""
    return db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE kind<>?",
        (_EarlySource.kind,),
    ).fetchone()[0]


async def _sweep(db, sources):
    async def deliver(*_args, **_kwargs):
        return True

    result = await runner.sweep(
        NOW,
        db=db,
        sources=sources,
        is_idle=lambda _thread_id: False,
        deliver=deliver,
    )
    await asyncio.sleep(0)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("with_early_source", [False, True], ids=["control", "earlier-enqueue"])
async def test_sweep_harvest_commits_and_notifies_once_with_trigger_parity(
    db, ws_registry, with_early_source,
):
    plan_id, node_id = _seed_attachment(db, "parent-a", "child-a")
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    sources = [HarvestSource()]
    if with_early_source:
        sources.insert(0, _EarlySource())

    await _sweep(db, sources)

    assert service.get_node(node_id, db=db).state == "review_requested"
    assert [message["type"] for message in socket.sent] == ["plans_changed"]
    assert socket.sent[0]["plans"][0]["plan_id"] == plan_id
    # Nothing routed: the child's review was its final return, which the
    # harvest carried, and its completion is a passive digest fact.
    assert _routed(db) == 0

    await _sweep(db, sources)
    assert [message["type"] for message in socket.sent] == ["plans_changed"]
    assert _routed(db) == 0


@pytest.mark.asyncio
async def test_sweep_without_harvest_emits_no_plan_nudge(db, ws_registry):
    _plan_id, node_id = _seed_attachment(
        db, "parent-a", "child-a", terminal=False,
    )
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]

    await _sweep(db, [_EarlySource(), HarvestSource()])

    assert service.get_node(node_id, db=db).state == "active"
    assert socket.sent == []


@pytest.mark.asyncio
async def test_prepare_batches_plans_by_authorized_user_and_dedupes_one_plan(
    db, ws_registry,
):
    plan_a, _ = _seed_attachment(db, "parent-a", "child-a1")
    same_plan, _ = _seed_attachment(db, "parent-a", "child-a2")
    plan_b, _ = _seed_attachment(db, "parent-b", "child-b")
    plan_c, _ = _seed_attachment(
        db,
        "parent-c",
        "child-c",
        user_id="user-2",
        project_id="project-2",
    )
    assert same_plan == plan_a
    user_one = _FakeSocket()
    user_two = _FakeSocket()
    ws_registry._ws_user_clients.update({
        "user-1": [user_one],
        "user-2": [user_two],
    })

    await _sweep(db, [HarvestSource()])

    assert len(user_one.sent) == len(user_two.sent) == 1
    user_one_plans = [item["plan_id"] for item in user_one.sent[0]["plans"]]
    assert len(user_one_plans) == 2
    assert set(user_one_plans) == {plan_a, plan_b}
    assert [item["plan_id"] for item in user_two.sent[0]["plans"]] == [plan_c]


@pytest.mark.asyncio
async def test_harvest_raise_precedes_collection_and_emits_nothing(
    db, ws_registry, monkeypatch,
):
    _plan_id, node_id = _seed_attachment(db, "parent-a", "child-a")
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]

    def fail_transition(*_args, **_kwargs):
        raise RuntimeError("harvest failed")

    monkeypatch.setattr(delegation.service, "_transition", fail_transition)
    with pytest.raises(RuntimeError, match="harvest failed"):
        await _sweep(db, [_EarlySource(), HarvestSource()])

    assert socket.sent == []
    assert service.get_node(node_id, db=db).state == "active"
    assert db.execute("SELECT COUNT(*) FROM continuation_triggers").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_harvest_commit_failure_rolls_back_and_emits_no_nudge(
    db, ws_registry,
):
    _plan_id, node_id = _seed_attachment(db, "parent-a", "child-a")
    socket = _FakeSocket()
    ws_registry._ws_user_clients["user-1"] = [socket]
    failing = _FailCommit(db)

    with pytest.raises(RuntimeError, match="commit failed"):
        await _sweep(failing, [HarvestSource()])

    assert socket.sent == []
    db.rollback()
    assert service.get_node(node_id, db=db).state == "active"


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_fail_committed_sweep(
    db, monkeypatch,
):
    _plan_id, node_id = _seed_attachment(db, "parent-a", "child-a")

    def fail_broadcast(*_args, **_kwargs):
        raise RuntimeError("socket unavailable")

    monkeypatch.setattr(notifications.state, "broadcast_to_user", fail_broadcast)

    result = await _sweep(db, [HarvestSource()])

    assert isinstance(result, dict)
    assert service.get_node(node_id, db=db).state == "review_requested"
