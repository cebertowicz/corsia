"""Start one Claude Code subagent child process."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

import subagents.cc_trust as cc_trust
from core.turn_core import BOOT_EPOCH
from infra.database import get_db
from infra.db_async import run_in_db
from infra.thread_driver import InputAdmission, admit_input
from infra.time import utc_now_iso
from llm.resolution import ExecutionTarget
from registry.cc_session import notify_cc_session, persist_cc_session
from subagents.cc_accept import require_owned_accept_db
from subagents.cc_invocation import CONFINEMENT_NONE, permission_mode_for
from subagents.cc_runtime_events import _make_on_event
from subagents.cc_runtime_helpers import (
    _validate_working_dir,
)
from subagents.cc_runtime_lifecycle import (
    finalize,
    handle_process_exit,
    settle_and_finalize_retired_live,
)
from subagents.cc_runtime_markers import (
    broadcast_subagent_marker,
    persist_subagent_marker,
)
from subagents.cc_runtime_quiescence import cleanup_ephemeral_live
from subagents.cc_runtime_state import Broadcast, _LIVE
from subagents.cc_spawn_prepare import CCSpawnRequest, PreparedCCSpawn, prepare_spawn
from subagents.cc_stream import CCStreamSession

log = logging.getLogger("subagents.cc_runtime")
#: Records a rule-started worker's id on its start before its first turn can run.
RuleStart = Callable[[str], Awaitable[None]]


def _admit_initial_task(prepared: PreparedCCSpawn) -> InputAdmission:
    """Place a spawned child's initial task in the canonical FIFO and Turn."""
    request = prepared.request
    admitted_at = utc_now_iso()
    admission = admit_input(
        prepared.child_id,
        role="user",
        content=request.task,
        intent="apply_now",
        message_id=str(uuid.uuid4()),
        turn_id=str(uuid.uuid4()),
        boot_epoch=BOOT_EPOCH,
        admitted_at=admitted_at,
        metadata={"cc": True, **prepared.parent_agent_metadata},
        delivery_options={
            "native_mode": "delegation",
            "attachments": [],
            "reasoning_effort": request.effort,
        },
    )
    if admission.batch is None:
        raise RuntimeError("fresh Claude Code child did not admit its initial task")
    return admission


async def _deliver_initial_task(
    prepared: PreparedCCSpawn,
    admission: InputAdmission,
) -> None:
    from infra.thread_input_types import FailedInputBatch

    if isinstance(admission.batch, FailedInputBatch):
        from infra.turn_admission_driver import drive_turn_admission

        await drive_turn_admission(admission.batch)
        raise RuntimeError("fresh Claude Code child failed plan admission")
    row = get_db().execute(
        "SELECT * FROM threads WHERE id=?", (prepared.child_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("spawned Claude Code child disappeared before delivery")
    from infra.thread_execution import execute_claimed_batch

    await execute_claimed_batch(
        admission.batch,
        thread_row=dict(row),
        target=prepared.request.target,
    )


async def _start_process(prepared: PreparedCCSpawn) -> None:
    request = prepared.request
    live = prepared.live
    _validate_working_dir(prepared.working_dir, request.project_id)
    from subagents.harness_tool_bridge import configure_cc_tool_bridge

    confined = request.confinement != CONFINEMENT_NONE
    mcp_config = None if confined else configure_cc_tool_bridge(
        live,
        thread_id=prepared.child_id,
        working_dir=prepared.working_dir,
        registry=prepared.tool_registry,
    )
    on_event = _make_on_event(live, prepared.child_id, prepared.bcast, None)

    async def on_exit(code: int | None, stderr: str) -> None:
        await handle_process_exit(
            prepared.child_id, live, code, stderr, prepared.bcast, None,
            finalize_callback=finalize,
        )

    session = CCStreamSession(
        binary=request.binary,
        session_id=prepared.child_id,
        seed_file=live.seed_file,
        mcp_config=mcp_config,
        permission_mode=permission_mode_for(request.confinement),
        confinement=request.confinement,
        add_dirs=request.add_dirs,
        working_dir=prepared.working_dir,
        claude_dir=prepared.resolved_claude_dir,
        model=prepared.cc_alias,
        effort=request.effort,
        on_event=on_event,
        on_exit=on_exit,
        corsia_thread_id=prepared.child_id,
    )
    live.session = session
    _previous_id, session_row = await run_in_db(
        persist_cc_session,
        prepared.child_id,
        prepared.child_id,
        title=prepared.name,
        cwd=prepared.working_dir,
        invocation=session.invocation(),
    )
    notify_cc_session(prepared.child_id, prepared.child_id, session_row)
    lock = _LIVE.lock(prepared.child_id)
    async with lock:
        _LIVE.publish(prepared.child_id, live, held=lock)
        await session.start()
        admission = await run_in_db(_admit_initial_task, prepared)
        marker_id = await run_in_db(
            persist_subagent_marker,
            request.parent_thread_id,
            prepared.child_id,
            prepared.name,
            None,
        )
        broadcast_subagent_marker(
            request.parent_thread_id,
            prepared.child_id,
            prepared.name,
            prepared.bcast,
            marker_id,
            None,
        )
    await _deliver_initial_task(prepared, admission)


async def _cleanup_failed_start(prepared: PreparedCCSpawn, error: BaseException) -> None:
    live = prepared.live
    child_id = prepared.child_id
    cc_trust.distrust(live)
    cleanup_ephemeral_live(live, thread_id=child_id)
    try:
        if _LIVE.get(child_id) is not live:
            cleanup_ephemeral_live(live, thread_id=child_id)
            return
        terminate = getattr(live.session, "terminate", None)
        if terminate is None:
            log.error("CC %s cannot prove startup process death; retaining guard", child_id)
            return
        try:
            await terminate()
        except BaseException:
            log.exception(
                "CC %s terminate failed during startup cleanup; retaining guard",
                child_id,
            )
            raise
        settled = await settle_and_finalize_retired_live(
            child_id, live, prepared.bcast, None, emit_closed=False
        )
        if not settled and _LIVE.get(child_id) is live:
            log.error("CC %s startup cleanup retained an unsettled guard", child_id)
    except BaseException as cleanup_error:
        if isinstance(error, asyncio.CancelledError):
            log.error(
                "CC %s startup cleanup failed while preserving caller cancellation: %s",
                child_id,
                cleanup_error,
            )
            raise error from cleanup_error
        raise


async def spawn(
    parent_thread_id: str,
    task: str | None,
    *,
    binary: str,
    target: ExecutionTarget,
    working_dir: str | None = None,
    effort: str | None = None,
    title: str | None = None,
    project_id: str | None = None,
    claude_dir: str | None = None,
    broadcast: Broadcast | None = None,
    account_id: str | None = None,
    confinement: str = CONFINEMENT_NONE,
    add_dirs: tuple[str, ...] = (),
    rule_start: RuleStart | None = None,
    db=None,
) -> str:
    """Spawn one Claude Code subagent child thread.

    A ``review`` confinement runs it read-only over ``add_dirs`` from its own
    empty working directory; the caller has already checked those reads.
    ``rule_start(child_id)`` records a rule-started worker before its first turn can run.
    """
    require_owned_accept_db(db)
    if not task:
        raise ValueError("spawn requires a task")
    request = CCSpawnRequest(
        parent_thread_id, task, binary, target, working_dir, effort, title,
        project_id, claude_dir, broadcast, account_id,
        confinement=confinement, add_dirs=add_dirs,
    )
    prepared = await prepare_spawn(request, get_db())
    try:
        if rule_start is not None:
            await rule_start(prepared.child_id)
        await _start_process(prepared)
        log.info(
            "Spawned CC subagent child=%s from parent=%s",
            prepared.child_id,
            parent_thread_id,
        )
        return prepared.child_id
    except BaseException as error:
        await _cleanup_failed_start(prepared, error)
        raise
