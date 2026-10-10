"""Claude Code stream-json control requests."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from automation.turn_end_effects import TurnEndEffects

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TerminalWithPlanEffects:
    """A terminal and its plan effects, delivered after the callback exits."""

    settlement: object
    effects: TurnEndEffects


async def record_tool_reply(session, call_id: str, content: str, target) -> None:
    """Await this call's complete receipt and count its actual outgoing reply."""
    while True:
        proc = session._proc
        if (session._closing or proc is None or proc.returncode is not None
                or (proc.stdout is not None and proc.stdout.at_eof())):
            raise RuntimeError("Claude process ended before tool context was measured")
        if session.tool_context.ready(call_id):
            break
        if session.tool_context.finished and call_id in session.tool_context.tool_ids:
            raise RuntimeError("Claude did not report complete tool context")
        session._context_changed.clear()
        await session._context_changed.wait()
    session.tool_context.target = target
    session.tool_context.accept_tool_result(content)


def _observe_terminal_driver(session, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    log.error(
        "cc_stream: terminal successor failed for %s",
        session.corsia_thread_id,
        exc_info=(type(error), error, error.__traceback__),
    )
    if session._proc is not None and session._proc.returncode is None:
        session._proc.kill()


def retain_terminal_driver(session, operation) -> asyncio.Task:
    """Retain and observe cleanup/settlement after its callback unregisters."""
    task = asyncio.create_task(operation)
    task.add_done_callback(lambda done: _observe_terminal_driver(session, done))
    session._terminal_driver = task
    return task


def start_terminal_driver(session, settlement) -> asyncio.Task:
    """Drive one returned terminal settlement after its stream callback exits."""
    from core.turn_core import drive_turn_settlement

    return retain_terminal_driver(session, drive_turn_settlement(settlement))


def observe_terminal_event(session, task: asyncio.Task[object | None]) -> None:
    """Drive committed terminals; kill only genuinely failed event delivery."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "cc_stream: terminal delivery failed for %s",
            session.corsia_thread_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        if session._proc is not None and session._proc.returncode is None:
            session._proc.kill()
        return
    terminal = task.result()
    if terminal is None:
        return
    effects = None
    if isinstance(terminal, TerminalWithPlanEffects):
        effects = terminal.effects
        terminal = terminal.settlement
    from subagents.cc_process_exit import _CommittedTerminalFailure

    if isinstance(terminal, _CommittedTerminalFailure):
        error = terminal.publication_error
        log.error(
            "cc_stream: terminal publication failed for %s",
            session.corsia_thread_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        terminal = terminal.settlement
    if effects is not None:
        from automation.turn_end_effects import deliver_turn_end_after
        from core.turn_core import drive_turn_settlement

        session._terminal_driver = retain_terminal_driver(
            session,
            deliver_turn_end_after(
                effects,
                lambda: drive_turn_settlement(terminal),
            ),
        )
        return
    session._terminal_driver = start_terminal_driver(session, terminal)


async def interrupt(session) -> bool:
    """Ask the live CLI to abort its in-flight turn."""
    proc = session._proc
    if (
        proc is None
        or proc.stdin is None
        or proc.returncode is not None
        or proc.stdin.is_closing()
    ):
        return False
    session._interrupt_seq += 1
    frame = json.dumps(
        {
            "type": "control_request",
            "request_id": f"corsia-intr-{session._interrupt_seq}",
            "request": {"subtype": "interrupt"},
        }
    )
    try:
        proc.stdin.write((frame + "\n").encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as exc:
        log.warning("cc_stream: interrupt write failed: %s", exc)
        return False
    return True


async def set_model(session, model: str, *, timeout: float) -> None:
    """Select a model on the live CLI and await its acknowledgement."""
    proc = session._proc
    if (
        proc is None
        or proc.stdin is None
        or proc.returncode is not None
        or proc.stdin.is_closing()
    ):
        raise RuntimeError("set_model requires a live Claude Code process")
    session._control_seq += 1
    request_id = f"corsia-set-model-{session._control_seq}"
    response = asyncio.get_running_loop().create_future()
    session._control_responses[request_id] = response
    frame = json.dumps(
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "set_model", "model": model},
        }
    )
    try:
        proc.stdin.write((frame + "\n").encode())
        await proc.stdin.drain()
        result = await asyncio.wait_for(asyncio.shield(response), timeout)
        if result.get("subtype") != "success":
            raise RuntimeError(f"Claude Code rejected model {model}: {result}")
        session.model = model
    finally:
        pending = session._control_responses.pop(request_id, None)
        if pending is not None and not pending.done():
            pending.cancel()


def resolve_response(session, raw: dict) -> bool:
    """Route a raw response to its waiting control request."""
    response = raw.get("response")
    if not isinstance(response, dict):
        return False
    request_id = response.get("request_id")
    if not isinstance(request_id, str):
        return False
    pending = session._control_responses.get(request_id)
    if pending is None or pending.done():
        return False
    pending.set_result(response)
    return True


def fail_pending(session, detail: str) -> None:
    """Fail controls that cannot receive an acknowledgement after exit."""
    for pending in session._control_responses.values():
        if not pending.done():
            pending.set_exception(RuntimeError(detail))
