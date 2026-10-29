"""Rule starts use the owner's model or refuse with a useful reason."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.rule_worker_helpers import OWNER, make_rule, request, starts, world, worker_events  # noqa: F401

LOCAL = "omlx@local/qwen"


@pytest.fixture
def local_model(world, monkeypatch):
    from infra.database import write_transaction
    from tools import delegate_task

    with write_transaction(None, scope="local_model") as db:
        db.execute("UPDATE models SET backend='network' WHERE id=?", (LOCAL,))
        db.execute("INSERT INTO network_servers(id,provider,kind,instance_id,instance_name,port,status,"
                   "metadata_json,last_seen_at,created_at,updated_at) VALUES "
                   "('local','omlx','managed','local','local',8000,'online','{\"private\": true}','t','t','t')")
        db.execute("UPDATE threads SET model=? WHERE id=?", (LOCAL, OWNER))
    state = {"status": "ready", "bound": []}

    def eligible(model, **_scope):
        state["bound"].append(model)
        return model, None

    monkeypatch.setattr("llm.model_status.scoped_status_rows", lambda **_: [{
        "id": LOCAL, "litellm_model": LOCAL, "display_name": "Local Qwen",
        "is_local": True, "status": state["status"],
    }])
    monkeypatch.setattr("llm.account_catalog.effective_status", lambda row: (row["status"], None))
    monkeypatch.setattr(delegate_task, "eligible_delegation_model", eligible)
    monkeypatch.setattr(delegate_task, "send_thread_input", AsyncMock(side_effect=_admit))
    return state


async def _admit(thread_id, *_args, **_kwargs):
    from infra.database import write_transaction
    from infra.db_async import run_in_db

    def write():
        with write_transaction(None, scope="rule_worker_admit") as db:
            db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,input_order,delivery_state) "
                       "VALUES (?,?,'user','brief','t',1,'queued')", (f"input-{thread_id}", thread_id))
    await run_in_db(write)
    return SimpleNamespace(driver="native", duplicate=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["thread", "project"])
async def test_ready_local_owner_starts_confined_on_own_model(local_model, world, policy):
    from automation import worker_runner
    from infra.database import get_db, write_transaction
    with write_transaction(None, scope="local_policy") as db:
        if policy == "thread":
            db.execute("UPDATE threads SET local_only=1 WHERE id=?", (OWNER,))
        else:
            db.execute("INSERT INTO project_inference_policies (project_id, cloud_models_off) VALUES (?,1)",
                       (world["project"],))
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 1
    outcome, _, _, worker = starts(rule)[0]
    assert outcome == "started"
    child = get_db().execute("SELECT model, confinement FROM threads WHERE id=?", (worker,)).fetchone()
    assert tuple(child) == (LOCAL, "untrusted")
    assert local_model["bound"] == [LOCAL]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["local-only", "cloud-off", "ordinary"])
async def test_unloaded_owner_model_is_refused_with_same_repair(local_model, world, monkeypatch, policy):
    from automation import worker_runner
    from infra.database import get_db, write_transaction
    from tools import delegate_task
    from events import intake
    original_admit = intake.admit_event
    admitted = []

    async def capture_event(**kwargs):
        if kwargs["event_type"] == "worker.no_result":
            admitted.append(kwargs["facts"])
        return await original_admit(**kwargs)

    monkeypatch.setattr(intake, "admit_event", capture_event)
    with write_transaction(None, scope="owner_unloaded") as db:
        if policy == "local-only":
            db.execute("UPDATE threads SET local_only=1 WHERE id=?", (OWNER,))
        elif policy == "cloud-off":
            db.execute("INSERT INTO project_inference_policies (project_id, cloud_models_off) VALUES (?,1)",
                       (world["project"],))
    local_model["status"] = "available"
    sent = AsyncMock()
    spawn = AsyncMock()
    monkeypatch.setattr(delegate_task, "send_thread_input", sent)
    monkeypatch.setattr(delegate_task, "_spawn", spawn)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert reason == "your model Local Qwen is not loaded. Load it, or choose a local model that is loaded."
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert admitted[0]["reason"] == f"refused: {reason}"
    assert not get_db().execute("SELECT 1 FROM threads WHERE spawned_by=?", (OWNER,)).fetchone()
    sent.assert_not_awaited()
    spawn.assert_not_awaited()
    assert local_model["bound"] == []


@pytest.mark.asyncio
async def test_available_status_is_reported_as_not_loaded(local_model):
    from automation import worker_runner
    local_model["status"] = "available"
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert reason == "your model Local Qwen is not loaded. Load it, or choose a local model that is loaded."


@pytest.mark.asyncio
async def test_native_workers_hold_slots_until_settled_and_mixed_handles_count_once(local_model, monkeypatch):
    from automation import worker_runner
    from events import worker_starts
    from infra.database import get_db, write_transaction

    monkeypatch.setattr("infra.subagent_caps.get_max_concurrent_live", lambda: 2)
    handles = set()
    monkeypatch.setattr("subagents.cc_idle_retirement.working_thread_ids", lambda **_: handles)
    rule = await make_rule()
    await request("first")
    await request("second")
    assert await worker_runner.drive() == 2
    workers = [row[3] for row in starts(rule)]
    handles.add(workers[0])
    assert worker_runner._free(get_db()) == 0
    await request("third")
    assert await worker_runner.drive() == 0
    assert starts(rule)[2][0] == "queued"
    with write_transaction(None, scope="settle_native") as db:
        decision = db.execute("SELECT id FROM event_route_decisions WHERE worker_thread_id=?", (workers[1],)).fetchone()[0]
        worker_starts.end(db, decision, "no_result")
    assert await worker_runner.drive() == 1
    assert starts(rule)[2][0] == "started"


@pytest.mark.asyncio
async def test_account_spawn_error_is_logged_and_refused_generically(local_model, monkeypatch):
    from automation import worker_runner
    from loguru import logger
    from tools import delegate_task

    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        monkeypatch.setattr(delegate_task, "_spawn", AsyncMock(return_value="Error: account unavailable"))
        rule = await make_rule()
        await request()
        assert await worker_runner.drive() == 0
        outcome, reason, _, worker = starts(rule)[0]
        assert outcome == "refused" and worker is None
        assert reason == "the start failed (delegation error)"
        assert "Choose another model" not in reason
        assert any("account unavailable" in message for message in messages)
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
async def test_missing_owner_model_refusal_says_how_to_fix_it(world):
    from automation import worker_runner
    from infra.database import write_transaction
    with write_transaction(None, scope="missing_model") as db:
        db.execute("UPDATE threads SET model=NULL WHERE id=?", (OWNER,))
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert "no model" in reason and "Choose" in reason
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]


@pytest.mark.asyncio
async def test_inherited_local_only_cloud_model_is_refused_with_repair(world, monkeypatch):
    from automation import worker_runner
    from infra.database import get_db, write_transaction
    from tools import delegate_task
    with write_transaction(None, scope="inherited_local") as db:
        db.execute("INSERT INTO threads (id,user_id,title,model,local_only,created_at,updated_at) "
                   "VALUES ('parent','user-1','Parent',?,1,'t','t')", (LOCAL,))
        db.execute("UPDATE threads SET spawned_by='parent', agent_enabled=1, local_only=1 WHERE id=?", (OWNER,))
    sent = AsyncMock()
    monkeypatch.setattr(delegate_task, "send_thread_input", sent)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert "Local only" in reason and "cloud" in reason and "Choose" in reason
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert not get_db().execute("SELECT 1 FROM threads WHERE spawned_by=?", (OWNER,)).fetchone()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_off_owner_cloud_model_is_refused_with_repair(world, monkeypatch):
    from automation import worker_runner
    from infra.database import get_db, write_transaction
    from tools import delegate_task
    with write_transaction(None, scope="cloud_off") as db:
        db.execute("INSERT INTO project_inference_policies (project_id, cloud_models_off) VALUES (?,1)",
                   (world["project"],))
    sent = AsyncMock()
    monkeypatch.setattr(delegate_task, "send_thread_input", sent)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert "project" in reason and "cloud" in reason and "Choose" in reason
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert not get_db().execute("SELECT 1 FROM threads WHERE spawned_by=?", (OWNER,)).fetchone()
    sent.assert_not_awaited()
