"""Exercise cold-build/install interleavings through durable Codex events."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from core.turn_core import TurnIdentity
from test_external_cold_tool_debt import _context, debt_db  # noqa: F401


def _codex_events(db):
    from subagents.codex_runtime_events import make_on_event
    from subagents.codex_runtime_state import _LiveCodex

    # Match the boundary's existing Step, so real persist_tool_use appends there.
    row = db.execute("SELECT metadata FROM messages WHERE id='older-step'").fetchone()
    metadata = json.loads(row["metadata"])
    metadata["provider_turn_id"] = "provider-turn"
    db.execute("UPDATE messages SET metadata=? WHERE id='older-step'", (json.dumps(metadata),))
    row = db.execute("SELECT metadata FROM messages WHERE id='older-result'").fetchone()
    if row is not None:
        metadata = json.loads(row["metadata"])
        metadata.update(provider_tool_call_id="older", tool_result_binding_state="bound")
        db.execute("UPDATE messages SET metadata=? WHERE id='older-result'", (json.dumps(metadata),))
    db.commit()
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=SimpleNamespace(active_turn_id="provider-turn"),
        parent_thread_id=None, name="test", corsia_turn=TurnIdentity("current", 1),
    )
    on_event = make_on_event(live, "debt", lambda *_: None, dead_set=set(), thread_registry={})
    return live, on_event


def _event(kind, call_id):
    return {
        "kind": kind, "id": call_id, "tool_use_id": call_id,
        "name": "bash", "input": {}, "content": "bridge rejected",
        "provider_thread_id": "provider-thread", "provider_turn_id": "provider-turn",
        "_corsia_turn_identity": TurnIdentity("current", 1),
    }


async def _install(admission, *, previous=False):
    from dataclasses import replace
    from core.checkpoint_capture import capture_and_install
    from core.fork_types import ProviderForkResult
    from infra import checkpoint_queries
    from infra.checkpoints import create_running
    from infra.db_async import run_in_db

    request = admission.request
    result = ProviderForkResult(raw_body="saved narrative", usage=None)
    if previous:
        predecessor = replace(
            request, id="previous", through_seq=1, through_message_id="input", mode="warm",
        )
        await run_in_db(create_running, predecessor)
        assert (await capture_and_install(predecessor.id, result)).status == "installed"
        request = replace(request, previous_generation_id=predecessor.id)
    existing = await run_in_db(checkpoint_queries.read, request.id)
    if existing is None:
        await run_in_db(create_running, request)
    else:
        assert existing.status == "running" and existing.mode == "cold"
    return await capture_and_install(request.id, result)


async def _prepare_racing(context, live, on_event, monkeypatch, *, deferred, required, call_id="late"):
    from core import checkpoint_cold_admission, checkpoint_render
    from subagents.harness_checkpoint_resume import _prepare_external_checkpoint

    entered, release = threading.Event(), threading.Event()
    original = checkpoint_cold_admission.build_checkpoint_admission

    def paused(**kwargs):
        # This seam is reached only AFTER the admission guard checked the real map.
        assert not live.tool_step_ids
        entered.set()
        assert release.wait(10), "event handler did not release cold builder"
        return original(**kwargs)

    monkeypatch.setattr(checkpoint_cold_admission, "build_checkpoint_admission", paused)
    prepared = _prepare_external_checkpoint(
        "race", context=context, live=live, identity=live.corsia_turn,
        warm_source=SimpleNamespace(harness="codex") if deferred else None,
        required=required,
    )
    if deferred:
        from core.checkpoint_cold_fallback import build_cold_request
        from infra.checkpoints import create_running
        from infra.db_async import run_in_db

        warm = await prepared
        await run_in_db(create_running, warm.request)
        prepared = build_cold_request(warm.provider_request)
    task = asyncio.create_task(prepared)
    try:
        assert await asyncio.to_thread(entered.wait, 10), "cold guard seam not reached"
        await on_event(_event("tool_use", call_id))
        assert live.tool_step_ids == {call_id: "older-step"}
    finally:
        release.set()
    result = await task
    if deferred:
        from dataclasses import replace

        # Real build_cold_request invoked same_model_cold and adopted the existing row.
        return replace(warm, request=replace(warm.request, mode="cold"), provider_request=result)
    return result


@pytest.mark.parametrize("deferred", [False, True], ids=["direct", "warm-correction"])
@pytest.mark.parametrize("required", [False, True], ids=["optional", "required"])
@pytest.mark.asyncio
async def test_declaration_after_guard_refuses_cold_install(debt_db, monkeypatch, deferred, required):
    from history.projection import render_portable_history
    from infra import checkpoint_queries

    context = _context(debt_db, monkeypatch, "codex", outstanding=False)
    live, on_event = _codex_events(debt_db)
    admission = await _prepare_racing(
        context, live, on_event, monkeypatch, deferred=deferred, required=required,
    )
    assert admission.request.mode == "cold" and admission.request.through_seq == 3
    # The cut remains legal, but its Step gained a durably declared unpaid call.
    render_portable_history("debt", db=debt_db, through=context.boundary, include_unfinished_turn=True)
    record = await _install(admission, previous=not deferred)
    assert record.status == "overtaken"
    assert "tool" in record.error
    previous = checkpoint_queries.current_installed("debt", db=debt_db)
    if deferred:
        assert previous is None
    else:
        assert previous.id == "previous" and previous.through_seq == 1
    assert debt_db.execute("SELECT count(*) FROM messages WHERE id='checkpoint-receipt:race'").fetchone()[0] == 0


@pytest.mark.parametrize("deferred", [False, True], ids=["direct", "warm-correction"])
@pytest.mark.asyncio
async def test_late_settlement_invalidates_frozen_cut_before_install(debt_db, monkeypatch, deferred):
    from core._tool_result_delivery_support import ToolResultBindingError
    from history.projection import render_portable_history
    from history.types import PortableHistoryError
    from infra import checkpoint_queries
    from subagents.codex_runtime_events import settle_missing_tool_results

    context = _context(debt_db, monkeypatch, "codex", outstanding=False)
    live, on_event = _codex_events(debt_db)
    admission = await _prepare_racing(
        context, live, on_event, monkeypatch, deferred=deferred, required=True,
    )
    with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
        await on_event(_event("tool_result", "late"))
    assert live.tool_step_ids == {"late": "older-step"}
    await settle_missing_tool_results(
        live=live, thread_id="debt", identity=live.corsia_turn, bcast=lambda *_args: None,
    )
    assert not live.tool_step_ids
    with pytest.raises(PortableHistoryError, match="history boundary is not"):
        render_portable_history("debt", db=debt_db, through=context.boundary, include_unfinished_turn=True)
    record = await _install(admission)
    assert record.status == "overtaken"
    assert "boundary" in record.error
    assert checkpoint_queries.current_installed("debt", db=debt_db) is None


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.asyncio
async def test_settled_external_cold_still_installs(debt_db, monkeypatch, harness):
    from subagents.harness_checkpoint_resume import _prepare_external_checkpoint

    context = _context(debt_db, monkeypatch, harness, outstanding=False)
    live = SimpleNamespace(tool_step_ids={}, session=SimpleNamespace())
    admission = await _prepare_external_checkpoint(
        "settled-install", context=context, live=live, identity=TurnIdentity("current", 1),
        warm_source=None,
    )
    assert (await _install(admission)).status == "installed"


@pytest.mark.parametrize("deferred", [False, True], ids=["direct", "warm-correction"])
@pytest.mark.asyncio
async def test_preexisting_precut_call_settling_after_admission_refuses_install(debt_db, monkeypatch, deferred):
    from infra import checkpoint_queries

    from infra.db_async import run_in_db
    from subagents.child_thread import persist_assistant_text
    from subagents.harness_checkpoint_context import read_external_checkpoint_context

    context = _context(debt_db, monkeypatch, "codex", outstanding=True)
    await run_in_db(persist_assistant_text, "debt", "latest", turn_id="current")
    context = read_external_checkpoint_context(
        "debt", context.target.catalog_id, "codex", current_turn_id="current",
    )
    # The cut is on a LATER group; an older result raises the cumulative head_max.
    assert context.boundary.seq == 5
    live, on_event = _codex_events(debt_db)
    admission = await _prepare_racing(
        context, live, on_event, monkeypatch, deferred=deferred, required=True, call_id="older",
    )
    assert admission.request.through_seq == 5
    await on_event(_event("tool_result", "older"))
    assert not live.tool_step_ids
    record = await _install(admission, previous=not deferred)
    assert record.status == "overtaken" and "boundary" in record.error
    previous = checkpoint_queries.current_installed("debt", db=debt_db)
    if deferred:
        assert previous is None
    else:
        assert previous.id == "previous" and previous.through_seq == 1
