from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4


def _codex_target(
    selector: str = "gpt-5-codex",
    *,
    context_window: int = 200_000,
):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=f"codex/{selector}",
        harness="codex",
        provider="codex",
        backend="codex",
        transport_model=selector,
        expected_reported_model=selector,
        context_window=context_window,
        available=True,
        availability_reason=None,
        display_name=selector,
        supports_reasoning=True,
    )


def _make_mock_app_session(thread_id: str = "app_thread_123"):
    s = MagicMock()
    s.codex_thread_id = thread_id
    s.returncode = None
    s.transport_generation_id = str(uuid4())
    s.start = AsyncMock(return_value=True)
    s.start_turn = AsyncMock(return_value=True)
    s.steer = AsyncMock(return_value=True)
    s.interrupt = AsyncMock(return_value=True)
    s.terminate = AsyncMock()
    return s


def _reset_runtime():
    from subagents import codex_runtime
    codex_runtime._LIVE.reset()
    codex_runtime._DEAD.clear()
    codex_runtime._CODEX_THREADS.clear()
    codex_runtime._PROVIDER_CONTEXT_UNTRUSTED.clear()
    codex_runtime._APP_SESSIONS.clear()


def _seed_runtime_sessions(*session_ids: str) -> None:
    """Give transport-focused tests real thread owners without shared state."""
    from infra.database import get_db

    db = get_db()
    try:
        for thread_id in session_ids:
            db.execute(
                "INSERT OR IGNORE INTO threads "
                "(id,user_id,model,created_at,updated_at) VALUES (?,?,?,'t','t')",
                (thread_id, "test-user", "codex/gpt-5-codex"),
            )
        db.commit()
    finally:
        db.close()


def _admit_active_turn(
    thread_id: str,
    *,
    turn_id: str | None = None,
    content: str = "input",
    settle_write: bool = True,
):
    """Admit and acknowledge the input owned by a direct Codex test handle."""
    from core.turn_core import BOOT_EPOCH
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    turn_id = turn_id or f"turn-{thread_id}"
    admission = admit_input(
        thread_id,
        role="user",
        content=content,
        intent="apply_now",
        message_id=f"test-input:{turn_id}",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    assert admission.batch is not None
    if settle_write:
        attempt_id = f"test-write:{turn_id}"
        begin_batch_write(
            admission.batch,
            attempt_id=attempt_id,
            started_at=utc_now_iso(),
        )
        settle_batch_write(attempt_id, outcome="accepted")
    return admission.batch.identity


class _FenceSession:
    """An app-server session that records the exact fence stages it was given."""

    def __init__(self, thread_id: str, *, request_idle: bool = False) -> None:
        self.codex_thread_id = f"provider-{thread_id}"
        self.active_turn_id = f"provider-turn-{thread_id}"
        self.returncode = None
        self.request_idle = asyncio.Event()
        if request_idle:
            self.request_idle.set()
        self.admission_open = True
        self.calls: list[str] = []
        self.interrupt_result = True

    def revoke_dynamic_tool_admission(self) -> None:
        self.calls.append("revoke")
        self.admission_open = False

    def restore_dynamic_tool_admission(self) -> None:
        self.calls.append("restore")
        self.admission_open = True

    async def wait_dynamic_tool_requests_idle(self) -> None:
        self.calls.append("wait_request")
        await self.request_idle.wait()

    async def interrupt(self, timeout: float = 30.0) -> bool:
        del timeout
        self.calls.append("interrupt")
        if isinstance(self.interrupt_result, BaseException):
            raise self.interrupt_result
        return bool(self.interrupt_result)

    async def terminate(self) -> None:
        self.calls.append("terminate")


def _active_live(thread_id: str, session: object, *, pending_tool: bool = True):
    """One registered Codex handle owning one durable active Turn."""
    from shared_send_fixtures import _insert_session
    from subagents import codex_runtime

    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(thread_id)
    live = codex_runtime._LiveCodex(
        codex_thread_id=f"provider-{thread_id}",
        session=session,
        parent_thread_id=None,
        name="codex",
        corsia_turn=identity,
        model="gpt-5-codex",
        requires_terminal_identity=True,
    )
    if pending_tool:
        live.tool_names["call-1"] = "write_file"
        live.tool_step_ids["call-1"] = "step-1"
        live.tools_settled.clear()
    codex_runtime._LIVE.put(thread_id, live)
    codex_runtime._APP_SESSIONS[thread_id] = session
    codex_runtime._CODEX_THREADS[thread_id] = live.codex_thread_id
    return live


def _active_live_with_persisted_tool(thread_id: str, session: object):
    """The same handle, owing one real declared call its durable result."""
    from subagents.child_thread import persist_tool_use

    live = _active_live(thread_id, session, pending_tool=False)
    step_id = persist_tool_use(
        thread_id,
        tool_id="call-1",
        name="write_file",
        arguments={"path": "one"},
        provider="codex",
        model="gpt-5-codex",
        turn_id=live.corsia_turn.turn_id,
    )
    live.tool_names["call-1"] = "write_file"
    live.tool_step_ids["call-1"] = step_id
    live.tools_settled.clear()
    return live, step_id


def _run(coro):
    return asyncio.run(coro)


async def locked_spawn(spawn, thread_id: str, *args, **kwargs):
    """Run one Codex spawn under its thread's transition lock, as production does."""
    from subagents import codex_runtime

    lock = codex_runtime._LIVE.lock(thread_id)
    async with lock:
        return await spawn(thread_id, *args, held_lock=lock, **kwargs)


def declare_codex_tool_step(
    session,
    call_id: str,
    *,
    name: str = "read_file",
    arguments: dict | None = None,
    persist: bool = True,
) -> str:
    """Declare one provider call's Assistant Step as Codex's runtime does.

    In production ``_on_tool_use`` persists the Step row on ``item/started``
    and records it in the live owner's ``tool_step_ids``, which
    ``bind_app_server_session`` shares with the session as
    ``declared_tool_step_ids``; the dynamic callback reads that map. Tests that
    drive a session or callback directly call this instead. ``persist=False``
    is only for tests that replace result persistence (``execute_tool``) and so
    never touch the Step row; its ID is then a stand-in.
    """
    if persist:
        from subagents.child_thread import persist_tool_use

        identity = session.corsia_turn_identity
        step_id = persist_tool_use(
            session.corsia_thread_id,
            turn_id=identity.turn_id if identity is not None else None,
            tool_id=call_id,
            name=name,
            arguments=arguments or {},
            provider="codex",
        )
    else:
        step_id = f"step-{call_id}"
    session.declared_tool_step_ids[call_id] = step_id
    return step_id
