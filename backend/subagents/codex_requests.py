"""Serialized Codex JSON-RPC request write and correlated response ownership."""
from __future__ import annotations

import asyncio
import json

from subagents.codex_app_server_errors import CodexAppServerError
from subagents.vendor_write_admission import require_session_write


async def request(session, method, params=None, *, on_write_observed=None, on_drain_confirmed=None):
    """Write one request; paid Turn writes revalidate the session's frozen account."""
    from subagents.codex_app_server import REQUEST_TIMEOUT, STEER_ACK_TIMEOUT, _STEER_NOT_WRITTEN

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
    try:
        async with session._write_lock:
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
            await session._proc.stdin.drain()
            if on_drain_confirmed is not None:
                on_drain_confirmed()
        try:
            timeout = STEER_ACK_TIMEOUT if method == "turn/steer" else REQUEST_TIMEOUT
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise CodexAppServerError(f"codex app-server request timed out: {method}", {"method": method})
    finally:
        session._pending_methods.pop(msg_id, None)
        pending = session._pending.pop(msg_id, None)
        if pending is fut and not fut.done():
            fut.cancel()
