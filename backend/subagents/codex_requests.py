"""Serialized Codex JSON-RPC request write and correlated response ownership."""
from __future__ import annotations

import asyncio
import json

from subagents.codex_app_server_errors import CodexAppServerError
from subagents.vendor_write_admission import require_session_write


async def request(session, method, params=None, *, on_write_observed=None, on_drain_confirmed=None):
    """Bound one request's lock, pipe drain and response with a single deadline."""
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
    deadline = asyncio.timeout(STEER_REQUEST_TIMEOUT if method == "turn/steer" else REQUEST_TIMEOUT)
    drained = False
    try:
        async with deadline:
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
                drained = True
                if on_drain_confirmed is not None:
                    on_drain_confirmed()
            return await fut
    except TimeoutError as error:
        if not deadline.expired():
            raise
        if not drained:
            # Keep on_exit enabled: its existing owner retires the failed generation.
            # Killing a stalled writer also releases other writers stuck in drain.
            session._kill_process()
        raise CodexAppServerError(
            f"codex app-server request timed out: {method}", {"method": method},
        ) from error
    finally:
        session._pending_methods.pop(msg_id, None)
        pending = session._pending.pop(msg_id, None)
        if pending is fut and not fut.done():
            fut.cancel()
