"""Deleting a thread deletes every temporary subagent beneath it (#2647)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

_DANGLING_SQL = (
    "SELECT COUNT(*) FROM threads AS child LEFT JOIN threads AS spawner "
    "ON spawner.id = child.spawned_by "
    "WHERE child.spawned_by IS NOT NULL AND spawner.id IS NULL"
)


def _threads(db, *rows: tuple[str, str | None, int]) -> None:
    db.executemany(
        "INSERT INTO threads "
        "(id, user_id, spawned_by, agent_enabled, model, created_at, updated_at) "
        "VALUES (?, 'user', ?, ?, 'native/test', 't', 't')",
        rows,
    )
    db.commit()


def _seed(*rows: tuple[str, str | None, int]) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        _threads(db, *rows)
    finally:
        db.close()


def _retain_agent_responsibility(thread_id: str) -> None:
    """Model a demoted Agent whose identity remains available for re-promotion."""
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET agent_responsibility = 'Retained responsibility' WHERE id = ?",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()


def _mark_legacy_demoted_agent(thread_id: str) -> None:
    """Model the durable state written by human demotion before retention shipped."""
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET agent_status = 'inactive', agent_responsibility = NULL, "
            "agent_paths = '[]', agent_domains = '[]' WHERE id = ?",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()


def _lineage() -> dict[str, str | None]:
    from infra.database import get_db

    db = get_db()
    try:
        assert db.execute(_DANGLING_SQL).fetchone()[0] == 0
        return {
            row["id"]: row["spawned_by"]
            for row in db.execute("SELECT id, spawned_by FROM threads")
        }
    finally:
        db.close()


def _turn(thread_id: str, turn_id: str, *, order: int, status: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO active_turns (id, thread_id, execution_epoch, model, status, "
            "started_at, boot_epoch, turn_order) "
            "VALUES (?, ?, 0, 'native/test', ?, 't', 'boot', ?)",
            (turn_id, thread_id, status, order),
        )
        db.commit()
    finally:
        db.close()


def _user_sends(thread_id: str) -> None:
    from infra.thread_driver import admit_input

    admit_input(
        thread_id, role="user", content="hello", intent="apply_now",
        message_id=f"user-{thread_id}", turn_id=f"user-turn-{thread_id}",
        boot_epoch="boot", admitted_at="t",
    )


def _plan_wake(db, recipient: str, subject: str) -> None:
    from plans import plan_wake

    plan_wake.append_input(
        db, recipient=recipient, message_id=f"plan-wake:child_returned:{subject}:1",
        content="Your worker stopped.", metadata={"event_type": "plan.child_returned"},
    )


class _Stops(list):
    """The members stopped, in order; ``effects`` maps a member to its stop's side effect."""

    def __init__(self) -> None:
        super().__init__()
        self.effects: dict = {}


@pytest.fixture
def stopped(isolated_db, monkeypatch):
    from api import thread_mutations

    stops = _Stops()

    async def _quiesce(thread_id, *, user_id):
        stops.append(thread_id)
        if thread_id in stops.effects:
            stops.effects[thread_id]()
        return {}

    monkeypatch.setattr(thread_mutations, "authenticate", lambda _token: {"id": "user"})
    monkeypatch.setattr(
        thread_mutations.runtime_boundary, "quiesce_thread_runtimes", _quiesce,
    )
    return stops


async def _delete(thread_id: str) -> None:
    from api import thread_mutations

    await thread_mutations.delete_thread(thread_id, "token")


async def _refused(thread_id: str) -> None:
    with pytest.raises(HTTPException) as refused:
        await _delete(thread_id)
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_deleting_a_subagent_with_a_live_spawner_deletes_its_children(stopped):
    _seed(("root", None, 0), ("delegator", "root", 0), ("worker", "delegator", 0))

    await _delete("delegator")

    assert stopped == ["delegator", "worker"]
    assert _lineage() == {"root": None}


@pytest.mark.asyncio
async def test_a_promoted_agent_under_a_deleted_subagent_keeps_its_subtree(stopped):
    _seed(
        ("root", None, 0), ("mid", "root", 0),
        ("agent", "mid", 1), ("agent-worker", "agent", 0),
    )

    await _delete("mid")

    assert stopped == ["mid"]
    assert _lineage() == {"root": None, "agent": None, "agent-worker": "agent"}


@pytest.mark.asyncio
async def test_a_demoted_agent_kept_for_repromotion_keeps_its_subtree(stopped):
    _seed(
        ("root", None, 0), ("mid", "root", 0),
        ("demoted-agent", "mid", 0), ("agent-worker", "demoted-agent", 0),
    )
    _retain_agent_responsibility("demoted-agent")

    await _delete("mid")

    assert stopped == ["mid"]
    assert _lineage() == {
        "root": None, "demoted-agent": None, "agent-worker": "demoted-agent",
    }


@pytest.mark.asyncio
async def test_a_legacy_demoted_agent_keeps_its_subtree(stopped):
    _seed(
        ("root", None, 0), ("mid", "root", 0),
        ("legacy-agent", "mid", 0), ("agent-worker", "legacy-agent", 0),
    )
    _mark_legacy_demoted_agent("legacy-agent")

    await _delete("mid")

    assert stopped == ["mid"]
    assert _lineage() == {
        "root": None, "legacy-agent": None, "agent-worker": "legacy-agent",
    }


@pytest.mark.asyncio
async def test_a_root_delete_removes_the_whole_temporary_family(stopped):
    _seed(("root", None, 0), ("a", "root", 0), ("b", "a", 0), ("c", "root", 0))

    await _delete("root")

    assert stopped[0] == "root" and stopped[-1] == "b"
    assert _lineage() == {}


@pytest.mark.asyncio
async def test_a_childs_stop_wake_cannot_start_a_turn_on_its_fenced_parent(stopped):
    from infra.database import write_transaction
    from infra.thread_inputs import start_idle_turn

    _seed(("parent", None, 0), ("child", "parent", 0))
    started = []

    def child_stop_wakes_parent() -> None:
        with write_transaction(None, scope="test_child_stop_wake") as db:
            _plan_wake(db, "parent", "child")
        started.append(start_idle_turn(
            "parent", turn_id="woken", boot_epoch="boot", started_at="t",
        ))

    stopped.effects["child"] = child_stop_wakes_parent

    await _delete("parent")

    assert started == [None]
    assert _lineage() == {}


@pytest.mark.asyncio
async def test_a_child_finishing_during_the_delete_sends_its_fenced_parent_nothing(stopped):
    from infra.database import get_db
    from infra.thread_driver import finish_turn

    _seed(("parent", None, 0), ("child", "parent", 0))
    _turn("child", "child-turn", order=1, status="running")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) "
            "VALUES ('answer', 'child', 'assistant', 'done', '{\"turn_id\": \"child-turn\"}', 't', 1)"
        )
        db.commit()
    finally:
        db.close()
    finished = []
    stopped.effects["child"] = lambda: finished.append(finish_turn(
        "child-turn", outcome="completed", ended_at="t",
        successor_turn_id="next", boot_epoch="boot",
    ).wake_thread_ids)

    await _delete("parent")

    assert finished == [()]
    assert _lineage() == {}


@pytest.mark.asyncio
async def test_user_input_to_a_member_during_the_delete_is_refused_visibly(stopped):
    from infra.send_types import SendRejected

    _seed(("parent", None, 0), ("child", "parent", 0))
    refusals = []

    def user_sends_to_each_member() -> None:
        for member in ("parent", "child"):
            try:
                _user_sends(member)
            except SendRejected as refused:
                refusals.append((member, refused.status_code, str(refused)))

    stopped.effects["child"] = user_sends_to_each_member

    await _delete("parent")

    assert refusals == [
        ("parent", 409, "thread is being deleted"),
        ("child", 409, "thread is being deleted"),
    ]
    assert _lineage() == {}


@pytest.mark.asyncio
async def test_a_running_member_refuses_the_whole_delete_and_lifts_the_fence(stopped):
    _seed(("parent", None, 0), ("child", "parent", 0))
    _turn("child", "child-turn", order=1, status="running")

    await _refused("parent")

    assert _lineage() == {"parent": None, "child": "parent"}
    _user_sends("parent")


def _stop_fails() -> None:
    from thread.model_state import ThreadQuiescenceError

    raise ThreadQuiescenceError("child did not stop")


@pytest.mark.parametrize("refusal", ["running_member", "stop_fails"])
@pytest.mark.asyncio
async def test_a_refused_delete_drives_the_family_it_fenced(stopped, monkeypatch, refusal):
    """Input queued behind the fence could not start a Turn, so it starts now."""
    from plans import public

    _seed(("parent", None, 0), ("child", "parent", 0))
    if refusal == "running_member":
        _turn("child", "child-turn", order=1, status="running")
    else:
        stopped.effects["child"] = _stop_fails
    driven = []

    async def drive(threads):
        driven.extend(threads)

    monkeypatch.setattr(public, "drive_threads", drive)

    await _refused("parent")

    assert driven == ["child", "parent"]
    assert _lineage() == {"parent": None, "child": "parent"}


@pytest.mark.asyncio
async def test_a_subagent_spawned_while_its_parent_stops_is_fenced_and_deleted(stopped):
    _seed(("parent", None, 0), ("child", "parent", 0))
    stopped.effects["parent"] = lambda: _seed(("late", "child", 0))

    await _delete("parent")

    assert stopped == ["parent", "child", "late"]
    assert _lineage() == {}


@pytest.mark.asyncio
async def test_a_member_that_joins_after_the_snapshot_refuses(stopped, monkeypatch):
    from api import thread_deletion

    _seed(("parent", None, 0), ("child", "parent", 0))
    snapshot = thread_deletion.family_versions

    def snapshot_then_join(family):
        versions = snapshot(family)
        _seed(("late", "child", 0))
        return versions

    monkeypatch.setattr(thread_deletion, "family_versions", snapshot_then_join)

    await _refused("parent")

    assert set(_lineage()) == {"parent", "child", "late"}


@pytest.mark.asyncio
async def test_the_woken_list_names_only_survivors(stopped, monkeypatch):
    """Each deleted member's end wakes its spawner; only the survivor is driven."""
    from plans import public

    _seed(("root", None, 0), ("delegator", "root", 0), ("worker", "delegator", 0))
    driven = []

    def wake_spawner(db, thread_id, *, reason):
        spawner = db.execute(
            "SELECT spawned_by FROM threads WHERE id = ?", (thread_id,),
        ).fetchone()[0]
        _plan_wake(db, spawner, thread_id)

    async def drive(threads):
        driven.extend(threads)

    monkeypatch.setattr(public, "on_child_thread_ended", wake_spawner)
    monkeypatch.setattr(public, "drive_threads", drive)

    await _delete("delegator")

    assert driven == ["root"]
    assert _lineage() == {"root": None}


def test_purge_keeps_the_spawner_row_so_no_spawned_by_dangles(isolated_db):
    """Purge tombstones a thread instead of deleting it (``core.thread_lifecycle``)."""
    from datetime import datetime, timezone

    from core.thread_lifecycle import _apply_purge
    from infra.database import get_db

    _seed(("parent", None, 0), ("child", "parent", 0))
    db = get_db()
    try:
        _apply_purge(db, "parent", datetime.now(timezone.utc))
        db.commit()
    finally:
        db.close()

    assert _lineage() == {"parent": None, "child": "parent"}


def _install_checkpoint(thread_id: str, needle: str) -> None:
    """Write one installed checkpoint, a staged edit, its entry index and continuity."""
    import json

    from infra.checkpoint_entry_index import index_checkpoint_entries
    from infra.database import get_db

    db = get_db()
    try:
        checkpoint_id = f"cp-{thread_id}"
        db.execute(
            "INSERT INTO checkpoints "
            "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
            " status,source_harness,source_model,writer_harness,writer_model,"
            " continuity_base_revision,continuity_base_snapshot_json,started_at,"
            " raw_result,continuity_snapshot_json,installed_at) "
            "VALUES (?,?,0,100,'m-100','pressure','cold','installed','native','m',"
            "'native','m',0,'[]','t',?,?,'t')",
            (
                checkpoint_id,
                thread_id,
                needle,
                json.dumps([
                    {
                        "entry_id": 1,
                        "field": "facts",
                        "body": {"statement": needle},
                        "revision": 1,
                    },
                ]),
            ),
        )
        index_checkpoint_entries(db, checkpoint_id)
        db.execute(
            "INSERT INTO checkpoint_staged_edits "
            "(checkpoint_id,edit_order,operation,entry_id,field,body) "
            "VALUES (?,1,'add',1,'facts',?)",
            (checkpoint_id, json.dumps({"statement": needle})),
        )
        db.execute(
            "INSERT INTO continuity_store "
            "(thread_id,history_epoch,revision,next_entry_id,updated_at) "
            "VALUES (?,0,1,2,'t')",
            (thread_id,),
        )
        db.execute(
            "INSERT INTO continuity_entries "
            "(thread_id,history_epoch,entry_id,field,body,revision,state,"
            "created_at,updated_at) VALUES (?,0,1,'facts',?,1,'active','t','t')",
            (thread_id, json.dumps({"statement": needle})),
        )
        db.commit()
    finally:
        db.close()


def _retained_rows(thread_id: str) -> tuple[int, ...]:
    """Rows still stored for a thread: checkpoints, staged edits, index, continuity."""
    from infra.database import get_db

    counts = (
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
        "SELECT COUNT(*) FROM checkpoint_staged_edits WHERE checkpoint_id = 'cp-' || ?",
        "SELECT COUNT(*) FROM checkpoint_entry_search WHERE thread_id = ?",
        "SELECT COUNT(*) FROM continuity_entries WHERE thread_id = ?",
        "SELECT COUNT(*) FROM continuity_store WHERE thread_id = ?",
    )
    db = get_db()
    try:
        return tuple(db.execute(sql, (thread_id,)).fetchone()[0] for sql in counts)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_deleting_a_thread_deletes_its_checkpoints_and_entry_index(stopped):
    """Deleting is not retention: the deleted thread's stored text goes with it (#2291)."""
    _seed(("parent", None, 0), ("child", "parent", 0))
    _install_checkpoint("parent", "deletednarrative")
    _install_checkpoint("child", "deletednarrativechild")
    assert _retained_rows("parent") == (1, 1, 1, 1, 1)

    await _delete("parent")

    assert _retained_rows("parent") == (0, 0, 0, 0, 0)
    assert _retained_rows("child") == (0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_deleting_a_thread_keeps_another_threads_checkpoints(stopped):
    _seed(("keep", None, 0), ("parent", None, 0), ("child", "parent", 0))
    _install_checkpoint("keep", "keptnarrative")
    _install_checkpoint("parent", "deletednarrative")

    await _delete("parent")

    assert _retained_rows("keep") == (1, 1, 1, 1, 1)
    assert _retained_rows("parent") == (0, 0, 0, 0, 0)
