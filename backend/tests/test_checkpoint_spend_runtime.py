"""Dispatch binding and accounting faults never change checkpoint execution."""

import asyncio

import pytest

from core import checkpoint_runtime
from core.fork_types import ProviderForkResult
from infra import checkpoints
from infra.database import get_db
from infra.turn_usage_receipts import PLAN_FIELDS
from tests.test_checkpoint_runtime import _checkpoint, _provider, _seed


@pytest.mark.asyncio
async def test_usage_callback_freezes_task_and_survives_terminal_failure(isolated_db, monkeypatch):
    from core import checkpoint_native_fork
    from plans import public

    _seed("spend")
    checkpoints.create_running(_checkpoint("spend", "cp-spend"))
    task_a = dict(zip(PLAN_FIELDS, ("plan", "task-a", "attempt-a", "task-a", "execution")))
    task_b = {**task_a, "plan_node_id": "task-b", "plan_attempt_id": "attempt-b"}
    selected = task_a
    monkeypatch.setattr(public, "binding_at_call_start", lambda _: dict(selected))

    async def invoke(request, _cancel, **_kwargs):
        nonlocal selected
        selected = task_b
        assert request.on_usage is not None
        await request.on_usage("first", "native/test", {
            "prompt_tokens": 100, "completion_tokens": 50, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total_tokens": 150, "cost_usd": 0.25,
        })
        raise RuntimeError("provider failed after paid call")

    monkeypatch.setattr(checkpoint_native_fork, "invoke_fork", invoke)
    with pytest.raises(RuntimeError, match="provider failed after paid call"):
        await checkpoint_runtime._invoke(_provider("spend", "cp-spend"), asyncio.Event())
    db = get_db()
    try:
        row = db.execute("SELECT * FROM llm_usage WHERE thread_id='spend'").fetchone()
        assert row["plan_node_id"] == "task-a"
        assert row["plan_attempt_id"] == "attempt-a"
        assert row["plan_commitment_id"] == "task-a"
        assert row["turn_id"] is None
        assert row["cost_usd"] == 0.25
    finally:
        db.close()


@pytest.mark.asyncio
async def test_accounting_failure_reports_defect_and_keeps_provider_body(isolated_db, monkeypatch):
    from core import checkpoint_native_fork
    from infra import checkpoint_usage
    from plans import public

    _seed("broken-spend")
    checkpoints.create_running(_checkpoint("broken-spend", "cp-broken"))
    monkeypatch.setattr(public, "binding_at_call_start", lambda _: None)
    defects = []
    monkeypatch.setattr(public, "report_usage_defect", lambda *_a, **kw: defects.append(kw))

    def broken(*_a, **_kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(checkpoint_usage, "record_call", broken)

    async def invoke(request, _cancel, **_kwargs):
        await request.on_usage("paid", "native/test", {"total_tokens": 150})
        return ProviderForkResult("keep paid checkpoint", None)

    monkeypatch.setattr(checkpoint_native_fork, "invoke_fork", invoke)
    result = await checkpoint_runtime._invoke(_provider("broken-spend", "cp-broken"), asyncio.Event())
    assert result.raw_body == "keep paid checkpoint"
    assert len(defects) == 1
    assert defects[0]["reason"] == "checkpoint_usage_failed"


@pytest.mark.asyncio
async def test_unreadable_binding_reports_defect_without_claiming_a_task(isolated_db, monkeypatch):
    from core import checkpoint_usage
    from plans import public

    _seed("unattributed")
    checkpoints.create_running(_checkpoint("unattributed", "cp-unattributed"))
    monkeypatch.setattr(public, "binding_at_call_start", lambda _: public.UNATTRIBUTED)
    defects = []
    monkeypatch.setattr(public, "report_usage_defect", lambda *_a, **kw: defects.append(kw))
    request = await checkpoint_usage.bind_usage(_provider("unattributed", "cp-unattributed"))
    await request.on_usage("paid", "native/test", {
        "prompt_tokens": 100, "completion_tokens": 50, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "total_tokens": 150, "cost_usd": 0.25,
    })
    db = get_db()
    try:
        row = db.execute("SELECT * FROM llm_usage WHERE thread_id='unattributed'").fetchone()
        assert all(row[field] is None for field in PLAN_FIELDS)
        assert row["total_tokens"] == 150
        assert len(defects) == 1
        assert defects[0]["reason"] == "checkpoint_usage_failed"
    finally:
        db.close()
