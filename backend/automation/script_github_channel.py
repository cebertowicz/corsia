"""Run-scoped GitHub broker endpoint for sandboxed scripts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from contextlib import asynccontextmanager

from connections.github_broker import GithubReadRefused, default_broker
from tools.unix_socket_policy import corsia_temp_root_for_bind

SOCKET_ENV = "CORSIA_SCRIPT_GITHUB_SOCKET"
log = logging.getLogger(__name__)


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 thread_id: str, run_id: str, rule_id: str | None) -> None:
    try:
        request = json.loads(await reader.readline())
        if not isinstance(request, list) or not all(isinstance(arg, str) for arg in request):
            raise ValueError("expected a list of arguments")
        log.info("script github read thread=%s run=%s rule=%s", thread_id, run_id, rule_id)
        try:
            result = await asyncio.to_thread(default_broker(), ["api", *request])
            response = {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        except GithubReadRefused as exc:
            response = {"code": 1, "stdout": "", "stderr": str(exc)}
        except (OSError, subprocess.TimeoutExpired):
            response = {"code": 1, "stdout": "", "stderr": "GitHub read failed"}
        data = json.dumps(response).encode()
        writer.write(len(data).to_bytes(8, "big") + data)
        await writer.drain()
    except (ValueError, UnicodeDecodeError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


@asynccontextmanager
async def github_channel(thread_id: str, run_id: str, rule_id: str | None = None):
    """Serve requests for this run until its script exits; access is scoped to its thread."""
    path = corsia_temp_root_for_bind(thread_id) / uuid.uuid4().hex[:10]
    active: set[asyncio.Task] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_serve(reader, writer, thread_id=thread_id, run_id=run_id, rule_id=rule_id))
        active.add(task)
        task.add_done_callback(active.discard)

    server = await asyncio.start_unix_server(connected, path=str(path))
    try:
        os.chmod(path, 0o600)
        yield path
    finally:
        server.close()
        await server.wait_closed()
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        path.unlink(missing_ok=True)
