"""A Local only owner's rule start runs on a ready local model, or waits held (§8.1, §7.4).

The owner's own model is cloud (Claude Code), so every case also proves the start is
never bound to it. A local start runs ``delegate_task``'s native spawn for real; only
the worker's first input is recorded instead of run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.rule_worker_helpers import OWNER, atool, make_rule, request, starts, world  # noqa: F401

LOCAL = "omlx@local/qwen"
HELD = "held: local only, no local model ready"


@pytest.fixture(params=["thread", "project"])
def local_only(request, world, monkeypatch):
    """The owner made Local only by its chat's setting or its project's cloud off; the catalog's
    ready local models are ``ready["models"]``."""
    from infra.database import write_transaction
    from tools import delegate_task

    with write_transaction(None, scope="rule_worker_local") as db:
        if request.param == "thread":
            db.execute("UPDATE threads SET local_only=1 WHERE id=?", (OWNER,))
        else:
            db.execute("INSERT INTO project_inference_policies (project_id, cloud_models_off) VALUES (?,1)",
                       (world["project"],))
    ready = {"models": ()}
    monkeypatch.setattr("infra.local_only.ready_local_selectors", lambda: ready["models"])
    monkeypatch.setattr("llm.locality.row_is_local",
                        lambda row, **_servers: str(row.get("litellm_model") or "").lower().startswith("omlx"))
    bound: list[str] = []

    def eligible(model, **_scope):
        # The test catalog's local rows are never ready; any cloud bind fails the test.
        bound.append(model)
        assert model in ready["models"], f"bound {model}, not a ready local model"
        return model, None

    monkeypatch.setattr(delegate_task, "eligible_delegation_model", eligible)
    sent = AsyncMock(side_effect=_admit)
    monkeypatch.setattr(delegate_task, "send_thread_input", sent)
    return SimpleNamespace(ready=ready, sent=sent, bound=bound)


async def _admit(thread_id: str, *_args, **_kwargs) -> SimpleNamespace:
    """The worker's first input, admitted and left waiting: it is working, not cut short."""
    from infra.database import write_transaction
    from infra.db_async import run_in_db

    def _write() -> None:
        with write_transaction(None, scope="rule_worker_admit") as db:
            db.execute("INSERT INTO messages (id, thread_id, role, content, created_at, input_order, "
                       "delivery_state) VALUES (?, ?, 'user', 'brief', 't', 1, 'queued')",
                       (f"input-{thread_id}", thread_id))

    await run_in_db(_write)
    return SimpleNamespace(driver="native", duplicate=False)


def _workers() -> list[tuple[str, str]]:
    from infra.database import get_db

    return [tuple(row) for row in get_db().execute(
        "SELECT id, model FROM threads WHERE spawned_by=?", (OWNER,))]


async def _told(rule: str, event: str) -> list[str]:
    explained = await atool(OWNER, operation="explain", event_id=event)
    return [d["explanation"] for d in explained["decisions"] if d["rule_id"] == rule]


# (a) no local model ready: queued and held, no thread, no cloud bind; explain says why.
@pytest.mark.asyncio
async def test_no_local_model_ready_holds_the_start_queued(local_only, world):
    from automation import worker_runner
    from infra.database import get_db

    rule = await make_rule()
    event = await request()
    assert await worker_runner.drive() == 0
    assert starts(rule) == [("queued", HELD, "pr-1@a1", None)]
    assert _workers() == [] and local_only.sent.await_count == 0
    assert await _told(rule, event) == [f"rule {rule} will start a worker for {OWNER} with it: {HELD}"]
    assert await worker_runner.drive() == 0  # a second pass changes nothing
    assert starts(rule) == [("queued", HELD, "pr-1@a1", None)]
    db = get_db()
    try:
        messages = db.execute("SELECT content FROM messages WHERE thread_id=? AND role='system'", (OWNER,)).fetchall()
    finally:
        db.close()
    assert [row[0] for row in messages if row[0].startswith("Held the start of ")] == [
        "Held the start of worker-flow: this chat runs only on local models, and none is ready. "
        "It starts as soon as one is."]
    assert world["driven"] == [OWNER]


@pytest.mark.asyncio
async def test_hold_notice_uses_workflow_title_when_present(local_only, monkeypatch):
    from automation import worker_runner
    from infra.database import get_db

    rule = await make_rule()
    await request()
    monkeypatch.setattr("events.rule_actions.reachable_workflows",
                        lambda *_args: {"worker-flow": SimpleNamespace(corsia={"workflow": {"title": "Review change"}})})
    await worker_runner.drive()
    db = get_db()
    try:
        messages = [row[0] for row in db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND role='system'", (OWNER,))]
    finally:
        db.close()
    assert "Held the start of Review change: this chat runs only on local models, and none is ready. It starts as soon as one is." in messages
    assert starts(rule)[0][:2] == ("queued", HELD)


# (b) the catalog reports a local model ready: the catalog signal runs a pass that starts it.
@pytest.mark.asyncio
async def test_a_catalog_change_starts_a_held_start_on_the_ready_local_model(local_only, monkeypatch):
    from automation import worker_runner
    from infra import app_loop

    rule = await make_rule()
    await request()
    await worker_runner.drive()
    assert starts(rule)[0][:2] == ("queued", HELD)

    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())
    local_only.ready["models"] = (LOCAL,)
    worker_runner.poke_on_catalog_change()
    for _ in range(200):
        # The worker is recorded before its first input is sent, so wait for the send.
        if local_only.sent.await_count:
            break
        await asyncio.sleep(0.02)
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert (outcome, reason) == ("started", None)
    assert _workers() == [(worker, LOCAL)]
    local_only.sent.assert_awaited_once()


# (b2) a restart: the hold outlives the process, the catalog change that freed it does not.
@pytest.mark.asyncio
async def test_a_restart_starts_a_held_start_once_a_local_model_is_ready(local_only, monkeypatch):
    from automation import worker_runner

    rule = await make_rule()
    await request()
    await worker_runner.drive()
    assert starts(rule)[0][:2] == ("queued", HELD)

    local_only.ready["models"] = (LOCAL,)
    monkeypatch.setitem(worker_runner._state, "checked_holds", False)  # a new process
    assert await worker_runner.drive() == 1
    [(outcome, reason, _identity, worker)] = starts(rule)
    assert (outcome, reason) == ("started", None)
    assert _workers() == [(worker, LOCAL)]


def test_the_catalog_signal_reaches_the_runner(monkeypatch):
    from automation import worker_runner
    from infra.state import ws

    calls: list[str] = []
    monkeypatch.setattr(ws, "broadcast_all", lambda _message: None)
    monkeypatch.setattr("infra.unavailable_model_recovery.schedule_unavailable_model_recovery",
                        lambda: calls.append("recovery"))
    monkeypatch.setattr(ws, "_model_change_callbacks", [])
    monkeypatch.setattr(worker_runner, "poke_on_catalog_change", lambda: calls.append("runner"))
    worker_runner.poke()
    ws.notify_models_changed()
    assert calls == ["recovery", "runner"]


def test_catalog_notification_module_does_not_import_automation():
    import ast
    from pathlib import Path
    from infra.state import ws

    tree = ast.parse(Path(ws.__file__).read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert not any(name == "automation" or name.startswith("automation.") for name in imports)


# (c) an explicit local owner model is honored; a cloud one still binds a ready local model.
@pytest.mark.asyncio
@pytest.mark.parametrize("owner_model", ["oMLX/qwen", "claude-code/sonnet"])
async def test_the_start_binds_local_honoring_a_local_owner_model(local_only, owner_model):
    from automation import worker_runner
    from infra.database import write_transaction

    local_only.ready["models"] = (LOCAL, "oMLX/qwen")
    with write_transaction(None, scope="rule_worker_owner_model") as db:
        db.execute("UPDATE threads SET model=? WHERE id=?", (owner_model, OWNER))
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    [(_worker, model)] = _workers()
    assert model == (owner_model if owner_model == "oMLX/qwen" else LOCAL)
    assert starts(rule)[0][:2] == ("started", None)


@pytest.mark.asyncio
async def test_owner_notice_names_the_local_model_used(local_only):
    from automation import worker_runner
    from infra.database import get_db, write_transaction

    local_only.ready["models"] = (LOCAL, "omlx@local/second")
    with write_transaction(None, scope="local_model_name") as db:
        db.execute("UPDATE models SET display_name='Local Qwen', alias=NULL WHERE id=?", (LOCAL,))
    await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    db = get_db()
    try:
        messages = [row[0] for row in db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND role='system'", (OWNER,))]
    finally:
        db.close()
    assert "The workflow worker started on Local Qwen." in messages


# (d) held starts keep their places and take no slot, and start oldest first once a local
# model is ready; the batch per pass is unchanged.
@pytest.mark.asyncio
async def test_held_starts_keep_their_order(local_only, monkeypatch):
    from automation import worker_runner

    monkeypatch.setattr(worker_runner, "BATCH", 2)
    rule = await make_rule()
    for name in ("first", "second", "third"):
        await request(name)
    await worker_runner.drive()
    await worker_runner.drive()
    assert [row[:3] for row in starts(rule)] == [
        ("queued", HELD, "first"), ("queued", HELD, "second"), ("queued", HELD, "third")]

    local_only.ready["models"] = (LOCAL,)
    from infra.db_async import run_in_db
    await run_in_db(worker_runner._release_local_holds)
    assert await worker_runner.drive() == 2  # the batch: the two oldest
    assert [row[:3] for row in starts(rule)] == [
        ("started", None, "first"), ("started", None, "second"), ("queued", None, "third")]
    assert await worker_runner.drive() == 1
    assert [row[0] for row in starts(rule)] == ["started"] * 3


@pytest.mark.asyncio
async def test_held_starts_never_block_a_later_unheld_one(world, monkeypatch):
    """A pass reads no held start, even when more than a batch precedes one runnable start."""
    from automation import worker_runner
    from infra.db_async import run_in_db

    monkeypatch.setattr(worker_runner, "BATCH", 2)
    rule = await make_rule()
    for name in ("held-1", "held-2", "held-3", "free"):
        await request(name)
    from infra.database import write_transaction
    with write_transaction(None, scope="second_owner") as db:
        db.execute("INSERT INTO threads (id,user_id,title,model,project_id,created_at,updated_at) "
                   "VALUES ('free-owner','user-1','Free','claude-code/sonnet',?,'t','t')", (world['project'],))
        db.execute("UPDATE event_route_decisions SET thread_id='free-owner' WHERE request_identity='free'")
        db.execute("UPDATE event_route_decisions SET reason=? WHERE request_identity LIKE 'held-%'", (HELD,))
    calls = []
    def locality(_conn, owner, _ready):
        calls.append(owner['id'])
        return False if owner['id'] == OWNER else None
    monkeypatch.setattr(worker_runner, "_local_model", locality)
    claimed = await run_in_db(worker_runner._claim)
    assert [row["request_identity"] for row, _input in claimed] == ["free"]
    assert calls == ['free-owner']
    assert [row[:3] for row in starts(rule)] == [
        ("queued", HELD, "held-1"), ("queued", HELD, "held-2"), ("queued", HELD, "held-3"),
        ("started", None, "free")]
