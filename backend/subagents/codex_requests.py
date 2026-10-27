"""Serialized Codex JSON-RPC request write and correlated response ownership."""
from __future__ import annotations

import asyncio
import json

from subagents.codex_app_server_errors import CodexAppServerError
from subagents.vendor_write_admission import require_session_write


async def drain(session, deadline: asyncio.Timeout) -> None:
    """Drain under the lock holder's deadline; only its own expiry kills."""
    try:
        await session._proc.stdin.drain()
    except asyncio.CancelledError:
        if deadline.expired():
            # Leave on_exit enabled for the existing retirement owner.
            session._kill_process()
        raise


async def bounded_drain(session, budget: float, write: str) -> None:
    """Drain under this write's own budget; its expiry kills and names the write."""
    try:
        async with asyncio.timeout(budget) as deadline:
            await drain(session, deadline)
    except TimeoutError as error:
        if not deadline.expired():
            raise
        raise CodexAppServerError(
            f"codex app-server stdin drain timed out: {write}", {"method": write},
        ) from error


async def notify(session, method: str, params: dict) -> None:
    """Serialize a notification and bound its own lock-held drain."""
    from subagents.codex_app_server import REQUEST_TIMEOUT

    if session._proc is None or session._proc.stdin is None:
        raise RuntimeError("CodexAppServerSession not started")
    async with session._write_lock:
        payload = {"method": method, "params": params}
        session._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await bounded_drain(session, REQUEST_TIMEOUT, method)


async def request(session, method, params=None, *, on_write_observed=None, on_drain_confirmed=None):
    """Give lock admission, own drain and response separate method budgets."""
    from subagents.codex_app_server import REQUEST_TIMEOUT, STEER_REQUEST_TIMEOUT, _STEER_NOT_WRITTEN

    if session._proc is None or session._proc.stdin is None:
        raise RuntimeError("CodexAppServerSession not started")
    if session._proc.returncode is not None:
        raise RuntimeError(f"codex app-server exited (returncode={session._proc.returncode})")
    msg_id = session._next_id
    session._next_id += 1
    fut = asyncio.get_running_loop().create_future()
    session._pending[msg_id] = fut
    session._pending_methods[msg_id] = method
    payload = {"id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    budget = STEER_REQUEST_TIMEOUT if method == "turn/steer" else REQUEST_TIMEOUT
    admission, response = asyncio.timeout(budget), None
    try:
        async with admission:
            await session._write_lock.acquire()
        try:
            if method == "turn/steer" and not session._dynamic_tool_requests_idle.is_set():
                return _STEER_NOT_WRITTEN
            if session._closing and method in {"thread/inject_items", "turn/start", "turn/steer"}:
                raise RuntimeError("Codex app-server terminated before provider write")
            if method in {"turn/start", "turn/steer"}:
                require_session_write(session, "codex")
            session.request_log.append({"id": msg_id, "method": method, "params": params or {}})
            session._proc.stdin.write((json.dumps(payload) + "\n").encode())
            if on_write_observed is not None:
                on_write_observed()
            await bounded_drain(session, budget, method)
            if on_drain_confirmed is not None:
                on_drain_confirmed()
        finally:
            session._write_lock.release()
        # Created here so the response budget starts after the drain.
        response = asyncio.timeout(budget)
        async with response:
            return await fut
    except TimeoutError as error:
        if not (admission.expired() or (response is not None and response.expired())):
            raise
        raise CodexAppServerError(
            f"codex app-server request timed out: {method}", {"method": method},
        ) from error
    finally:
        session._pending_methods.pop(msg_id, None)
        pending = session._pending.pop(msg_id, None)
        if pending is fut and not fut.done():
            fut.cancel()
