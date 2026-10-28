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
    state = {"eligible": True, "bound": []}

    def eligible(model, **_scope):
        state["bound"].append(model)
        return (model, None) if state["eligible"] else (None, f"model {model} is not loaded")

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
    assert tuple(child) == (LOCAL, "review")
    assert local_model["bound"] == [LOCAL]


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [True, False], ids=["local-only", "ordinary"])
async def test_unloaded_owner_model_is_refused_with_same_repair(local_model, monkeypatch, restricted):
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
        db.execute("UPDATE threads SET local_only=? WHERE id=?", (int(restricted), OWNER))
    local_model["eligible"] = False
    sent = AsyncMock()
    monkeypatch.setattr(delegate_task, "send_thread_input", sent)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert reason == f"your model {LOCAL} is not loaded. Load it, or choose a local model that is loaded."
    assert [kind for kind, _ in worker_events()] == ["worker.no_result"]
    assert admitted[0]["reason"] == f"refused: {reason}"
    assert not get_db().execute("SELECT 1 FROM threads WHERE spawned_by=?", (OWNER,)).fetchone()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_eligibility_available_status_is_reported_as_not_loaded(local_model, monkeypatch):
    from automation import worker_runner
    from llm import delegation_model
    from tools import delegate_task
    monkeypatch.setattr(delegate_task, "eligible_delegation_model", delegation_model.eligible_delegation_model)
    monkeypatch.setattr(delegation_model, "_scoped_rows", lambda *_: ([{"id": LOCAL, "status": "available"}], None))
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    outcome, reason, _, worker = starts(rule)[0]
    assert outcome == "refused" and worker is None
    assert reason == f"your model {LOCAL} is not loaded. Load it, or choose a local model that is loaded."


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
