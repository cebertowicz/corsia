"""Rule starts wait for their owner's own local model, without blocking the queue."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.rule_worker_helpers import OWNER, make_rule, request, starts, world  # noqa: F401

LOCAL = "omlx@local/qwen"
WAIT = "waiting: local only, model not ready"


@pytest.fixture(params=["thread", "project"])
def local_only(request, world, monkeypatch):
    from infra.database import write_transaction
    from tools import delegate_task

    with write_transaction(None, scope="rule_worker_local") as db:
        if request.param == "thread":
            db.execute("UPDATE threads SET local_only=1 WHERE id=?", (OWNER,))
        else:
            db.execute("INSERT INTO project_inference_policies (project_id, cloud_models_off) VALUES (?,1)",
                       (world["project"],))
        db.execute("UPDATE threads SET model=? WHERE id=?", (LOCAL, OWNER))
    ready = {"models": (), "eligible": True, "bound": []}
    monkeypatch.setattr("infra.local_only.ready_local_selectors", lambda: ready["models"])
    monkeypatch.setattr("infra.local_only._is_cloud", lambda model, **_: not model.startswith("omlx"))

    def eligible(model, **_scope):
        ready["bound"].append(model)
        return (model, None) if ready["eligible"] else (None, "model unavailable")

    monkeypatch.setattr(delegate_task, "eligible_delegation_model", eligible)
    monkeypatch.setattr(delegate_task, "send_thread_input", AsyncMock(side_effect=_admit))
    return SimpleNamespace(kind=request.param, **ready, state=ready)


async def _admit(thread_id, *_args, **_kwargs):
    from infra.database import write_transaction
    from infra.db_async import run_in_db

    def write():
        with write_transaction(None, scope="rule_worker_admit") as db:
            db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,input_order,delivery_state) "
                       "VALUES (?,?,'user','brief','t',1,'queued')", (f"input-{thread_id}", thread_id))
    await run_in_db(write)
    return SimpleNamespace(driver="native", duplicate=False)


def messages():
    from infra.database import get_db
    db = get_db()
    try:
        return [r[0] for r in db.execute("SELECT content FROM messages WHERE thread_id=? AND role='system'", (OWNER,))]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wait_once_then_owner_local_model_starts(local_only, world):
    from automation import worker_runner
    from infra.database import get_db
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    assert await worker_runner.drive() == 0
    assert starts(rule)[0][:2] == ("queued", WAIT)
    assert not local_only.state["bound"]
    notices = [m for m in messages() if "is waiting to start" in m]
    policy = ("this project uses only local models" if local_only.kind == "project"
              else "this chat runs only on local models")
    assert len(notices) == 1
    assert notices[0] == (f"worker-flow is waiting to start: {LOCAL} isn't ready to run, and {policy}. "
                          f"It starts once {LOCAL} is ready.")
    assert world["driven"] == [OWNER]
    local_only.state["models"] = (LOCAL,)
    assert await worker_runner.drive() == 1
    outcome, reason, _, worker = starts(rule)[0]
    assert (outcome, reason) == ("started", None)
    db = get_db()
    try:
        assert db.execute("SELECT model FROM threads WHERE id=?", (worker,)).fetchone()[0] == LOCAL
    finally:
        db.close()


@pytest.mark.asyncio
async def test_ready_local_catalog_id_resolves_once_per_owner(local_only, monkeypatch):
    from automation import worker_runner
    from infra.database import write_transaction
    from llm import resolution
    with write_transaction(None, scope="model_id") as db:
        db.execute("UPDATE models SET id='local-id' WHERE litellm_model=?", (LOCAL,))
        db.execute("UPDATE threads SET model='local-id' WHERE id=?", (OWNER,))
    local_only.state["models"] = (LOCAL,)
    monkeypatch.setattr(worker_runner, "_free", lambda _: 2)
    calls = []
    original = resolution.catalog_row
    def lookup(model, *, db):
        calls.append(model)
        return original(model, db=db)
    monkeypatch.setattr(resolution, "catalog_row", lookup)
    rule = await make_rule()
    await request("one")
    await request("two")
    claimed = worker_runner._claim()
    assert len(claimed) == 2
    assert calls == ["local-id"]
    assert [row[0] for row in starts(rule)] == ["started", "started"]


@pytest.mark.asyncio
async def test_wait_notice_uses_workflow_title(local_only, monkeypatch):
    from automation import worker_runner
    await make_rule()
    await request()
    monkeypatch.setattr("events.rule_actions.reachable_workflows", lambda *_: {
        "worker-flow": SimpleNamespace(corsia={"workflow": {"title": "Review change"}})})
    await worker_runner.drive()
    assert any(m.startswith("Review change is waiting to start:") for m in messages())


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-code/sonnet", None])
async def test_wait_notice_cloud_or_no_model(local_only, model):
    from automation import worker_runner
    from infra.database import write_transaction
    with write_transaction(None, scope="owner_model") as db:
        db.execute("UPDATE threads SET model=? WHERE id=?", (model, OWNER))
    await make_rule()
    await request()
    await worker_runner.drive()
    notice, = [m for m in messages() if "is waiting to start" in m]
    policy = ("this project uses only local models" if local_only.kind == "project"
              else "this chat runs only on local models")
    if model:
        suffix = f"{model} is a cloud model"
    else:
        suffix = ("this chat" if local_only.kind == "project" else "it") + " has no model chosen"
    assert notice == (f"worker-flow is waiting to start: {policy}, and {suffix}. "
                      "It starts once this chat uses a local model that's ready.")
    assert not local_only.state["bound"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [True, False])
async def test_disabling_policy_tries_owner_model(local_only, world, ready):
    from automation import worker_runner
    from infra.database import write_transaction
    rule = await make_rule()
    await request()
    await worker_runner.drive()
    with write_transaction(None, scope="allow_cloud") as db:
        if local_only.kind == "thread":
            db.execute("UPDATE threads SET local_only=0 WHERE id=?", (OWNER,))
        else:
            db.execute("UPDATE project_inference_policies SET cloud_models_off=0 WHERE project_id=?", (world["project"],))
    local_only.state["eligible"] = ready
    assert await worker_runner.drive() == int(ready)
    assert starts(rule)[0][0] == ("started" if ready else "refused")
    assert local_only.state["bound"] == [LOCAL]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition,reason", [("disabled", "turned off"), ("gone", "owner gone"), ("paused", "owner paused")])
async def test_waiting_row_is_still_skipped(local_only, monkeypatch, condition, reason):
    from automation import worker_runner
    from events import rule_runs
    from infra.database import write_transaction
    rule = await make_rule()
    await request()
    await worker_runner.drive()
    if condition == "disabled":
        with write_transaction(None, scope="disable") as db:
            db.execute("UPDATE event_rules SET enabled=0 WHERE id=?", (rule,))
    elif condition == "gone":
        with write_transaction(None, scope="purge_owner") as db:
            db.execute("UPDATE threads SET lifecycle_state='purged' WHERE id=?", (OWNER,))
    else:
        monkeypatch.setattr("plans.public.thread_paused_since", lambda *_: "t")
    await worker_runner.drive()
    expected = {"disabled": rule_runs.TURNED_OFF, "gone": rule_runs.OWNER_GONE, "paused": rule_runs.OWNER_PAUSED}
    assert starts(rule)[0][:2] == ("skipped", expected[condition])


@pytest.mark.asyncio
async def test_all_waiting_rows_include_disabled_rule(local_only):
    from automation import worker_runner
    from events import rule_runs
    from infra.database import write_transaction
    first = await make_rule()
    second = await make_rule()
    await request()
    await worker_runner.drive()
    with write_transaction(None, scope="disable_rule") as db:
        db.execute("UPDATE event_rules SET enabled=0 WHERE id=?", (second,))
    await worker_runner.drive()
    assert starts(first)[0][:2] == ("queued", WAIT)
    assert starts(second)[0][:2] == ("skipped", rule_runs.TURNED_OFF)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 12])
async def test_waiting_rows_do_not_block_later_start(local_only, world, monkeypatch, count):
    from automation import worker_runner
    from infra.database import write_transaction
    monkeypatch.setattr(worker_runner, "BATCH", 2)
    monkeypatch.setattr(worker_runner, "_free", lambda _: 1)
    rule = await make_rule()
    for i in range(count):
        await request(f"wait-{i}")
    await request("run")
    with write_transaction(None, scope="second_owner") as db:
        db.execute("INSERT INTO threads (id,user_id,title,model,project_id,created_at,updated_at) "
                   "VALUES ('free-owner','user-1','Free','claude-code/sonnet',NULL,'t','t')")
        db.execute("UPDATE event_route_decisions SET thread_id='free-owner' WHERE request_identity='run'")
    claimed = worker_runner._claim()
    assert [r["request_identity"] for r, _ in claimed] == ["run"]
    assert all(r[:2] == ("queued", WAIT) for r in starts(rule)[:-1])


@pytest.mark.asyncio
async def test_notice_failure_does_not_prevent_claimed_start(local_only, monkeypatch):
    from automation import worker_runner, worker_notices
    from infra.database import write_transaction
    monkeypatch.setattr(worker_runner, "_free", lambda _: 1)
    rule = await make_rule()
    await request("wait")
    await request("run")
    with write_transaction(None, scope="second_owner") as db:
        db.execute("INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
                   "VALUES ('free-owner','user-1','Free','claude-code/sonnet','t','t')")
        db.execute("UPDATE event_route_decisions SET thread_id='free-owner' WHERE request_identity='run'")
    order = []
    logs = []
    monkeypatch.setattr(worker_runner.logger, "exception", lambda *args: logs.append(args))
    async def start(row, _input):
        order.append("start")
        return 1
    def notice(_row):
        order.append("notice")
        raise RuntimeError("notice failed")
    monkeypatch.setattr(worker_runner, "_start", start)
    monkeypatch.setattr(worker_notices, "notice_wait", notice)
    assert await worker_runner.drive() == 1
    assert order == ["start", "notice"]
    assert logs and "worker wait notice failed" in logs[0][0]
    assert starts(rule)[1][0] == "started"


@pytest.mark.asyncio
async def test_notice_drive_failure_is_logged_and_pass_continues(local_only, monkeypatch):
    from automation import script_runner, worker_runner
    logs = []
    monkeypatch.setattr(worker_runner.logger, "exception", lambda *args: logs.append(args))
    async def fail(_owner):
        raise RuntimeError("drive failed")
    monkeypatch.setattr(script_runner, "_drive_owner", fail)
    rule = await make_rule()
    await request()
    assert await worker_runner.drive() == 0
    assert starts(rule)[0][:2] == ("queued", WAIT)
    assert logs and "worker wait notice drive failed" in logs[0][0]
