"""A send in a cloud-off project is held at the FIFO head like any recoverable model state.

The person chose cloud-off, so the input waits (never fails) until they turn cloud back on or
move the thread to a local model, and the claim that releases it reads the same project policy
that held it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from tests.unavailable_model_support import HELD, message_row, seed_model

PROJECT = "proj-off"
CLOUD, LOCAL = "anthropic/x", "omlx@loop/m"


@pytest.fixture
def world(isolated_db, monkeypatch):
    """A cloud-off project with one ready cloud model and one ready loopback model."""
    import infra.thread_execution as execution
    import infra.unavailable_model_recovery as recovery
    from infra.database import get_db
    from llm.locality import set_project_cloud_models_off

    monkeypatch.setattr("llm.model_status._cloud_providers_active", lambda: {"anthropic"})
    monkeypatch.setattr("llm.model_status._network_server_status", lambda *_a: {"loop": "online", "lan": "online"})
    monkeypatch.setattr("llm.model_status._loaded_model_ids_fn", lambda: lambda _sid: set())
    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", Mock())
    seen = {"executed": AsyncMock()}
    monkeypatch.setattr(execution, "_execute_claimed_batch", seen["executed"])
    seed_model(CLOUD, backend="api", provider="anthropic")
    seed_model(LOCAL)
    db = get_db()
    try:
        db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
                   (PROJECT, PROJECT, "/tmp/" + PROJECT))
        db.execute(
            "INSERT INTO network_servers(id,provider,kind,instance_id,instance_name,port,status,metadata_json,"
            "last_seen_at,created_at,updated_at) VALUES ('loop','omlx','managed','loop','loop',8000,'online','{}','t','t','t')")
        db.execute("INSERT INTO network_server_endpoints(id,server_id,host,kind,created_at,updated_at)"
                   " VALUES ('ep','loop','127.0.0.1','local','t','t')")
        db.execute("INSERT INTO threads(id,user_id,model,project_id,created_at,updated_at)"
                   " VALUES ('t','user-1',?,?,'t0','t0')", (CLOUD, PROJECT))
        db.commit()
        set_project_cloud_models_off(PROJECT, cloud_models_off=True, db=db)
    finally:
        db.close()
    return seen


def _admit(message_id: str, content: str = "hello"):
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        return admit_input(
            "t", role="user", content=content, intent="apply_now",
            message_id=message_id, turn_id=f"{message_id}-turn",
            boot_epoch="boot-1", admitted_at="t1", db=db,
        )
    finally:
        db.close()


def _count(table: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        db.close()


def _turn_rows() -> list[tuple]:
    from infra.database import get_db

    db = get_db()
    try:
        return [tuple(r) for r in db.execute("SELECT id,status FROM active_turns ORDER BY id").fetchall()]
    finally:
        db.close()


@pytest.fixture
def turn_ends(monkeypatch):
    import core.plan_turn_end as plan_turn_end

    reported = AsyncMock()
    monkeypatch.setattr(plan_turn_end, "record_turn_end_logged", reported)
    return reported


async def _send_and_hold(message_id: str = "m1"):
    from infra.thread_execution import drive_claimed_batch

    admission = _admit(message_id)
    assert admission.batch is not None
    held = await drive_claimed_batch(admission.batch)
    assert held.receipt.message_id == message_id
    return held


def _set_cloud_off(value: bool) -> None:
    from infra.database import get_db
    from llm.locality import set_project_cloud_models_off

    db = get_db()
    try:
        set_project_cloud_models_off(PROJECT, cloud_models_off=value, db=db)
    finally:
        db.close()


def _set_thread_model(model: str) -> None:
    """What a committed model switch leaves behind: the thread's model and its queued rows' target."""
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("UPDATE threads SET model=? WHERE id='t'", (model,))
        db.execute("UPDATE messages SET target_model=? WHERE thread_id='t' AND delivery_state='queued'", (model,))
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_cloud_off_send_is_held_not_failed_and_names_the_cause(world, turn_ends) -> None:
    await _send_and_hold()

    assert message_row("m1") == HELD
    turn_ends.assert_awaited_once()
    kwargs = turn_ends.await_args.kwargs
    assert kwargs["outcome"] == "error" and kwargs["turn_id"] == "m1-turn"
    assert "this project uses local models only" in kwargs["error"] and CLOUD in kwargs["error"]
    world["executed"].assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_catalog_changes_neither_reclaim_nor_rehold(world, turn_ends) -> None:
    import infra.unavailable_model_recovery as recovery

    await _send_and_hold()
    before = (_turn_rows(), _count("messages"))

    await recovery._resume_held_heads()
    await recovery._resume_held_heads()

    assert (_turn_rows(), _count("messages")) == before
    assert message_row("m1") == HELD
    turn_ends.assert_awaited_once()
    world["executed"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_held_head_is_still_the_one_the_hold_selectors_find(world) -> None:
    from infra.unavailable_model_recovery import _held_thread_ids

    await _send_and_hold()

    assert _held_thread_ids() == ["t"]


def _sent(claimed) -> list[str]:
    """The person's inputs in a claimed batch, in order (the context digest rides along)."""
    return [item.message_id for item in claimed.items if item.message_id.startswith("m")]


@pytest.mark.asyncio
async def test_turning_cloud_back_on_releases_the_head(world, turn_ends) -> None:
    import infra.unavailable_model_recovery as recovery

    await _send_and_hold()
    _set_cloud_off(False)

    await recovery._resume_held_heads()

    world["executed"].assert_awaited_once()
    claimed = world["executed"].await_args.args[0]
    assert _sent(claimed) == ["m1"]
    assert message_row("m1")[2] is None


@pytest.mark.asyncio
async def test_moving_the_thread_to_a_local_model_releases_the_head_in_fifo_order(world, turn_ends) -> None:
    import infra.unavailable_model_recovery as recovery

    await _send_and_hold("m1")
    later = _admit("m2", "second")
    assert later.batch is None, "a later input waits behind the held head"
    _set_thread_model(LOCAL)

    await recovery._resume_held_heads()

    world["executed"].assert_awaited_once()
    claimed = world["executed"].await_args.args[0]
    assert _sent(claimed) == ["m1", "m2"]
    assert message_row("m1")[2] is None


@pytest.mark.asyncio
async def test_a_private_mark_on_a_lan_server_releases_the_head_through_the_models_changed_path(world, turn_ends) -> None:
    import infra.unavailable_model_recovery as recovery
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("INSERT INTO network_servers(id,provider,kind,instance_id,instance_name,port,status,metadata_json,"
                   "last_seen_at,created_at,updated_at) VALUES ('lan','omlx','managed','lan','lan',8000,'online','{}','t','t','t')")
        db.execute("INSERT INTO network_server_endpoints(id,server_id,host,kind,created_at,updated_at)"
                   " VALUES ('ep2','lan','192.168.1.2','lan','t','t')")
        db.commit()
    finally:
        db.close()
    seed_model("omlx@lan/n")
    await _send_and_hold()
    _set_thread_model("omlx@lan/n")
    await recovery._resume_held_heads()
    assert message_row("m1") == HELD, "an unmarked LAN server is not local, so cloud-off still holds"

    db = get_db()
    try:
        db.execute("UPDATE network_servers SET metadata_json='{\"private\": true}' WHERE id='lan'")
        db.commit()
    finally:
        db.close()
    await recovery._resume_held_heads()

    world["executed"].assert_awaited_once()
    assert message_row("m1")[2] is None
