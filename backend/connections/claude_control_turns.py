"""Talking to a Claude Code control session one request at a time."""

from __future__ import annotations

import asyncio
import json
from typing import Any


async def converse(process: asyncio.subprocess.Process, frames: list[dict[str, Any]]) -> tuple[bytes, bytes]:
    """Write each frame once the one before it was answered; return what the CLI wrote.

    ``end_session`` is the one request that is not waited on. A refused request,
    or a CLI that closes its output before answering, stops the conversation
    there: the output so far is what the caller reads its reason from.
    """
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    reader = process.stdout
    stdout = bytearray()
    scanned = 0
    stderr = asyncio.ensure_future(process.stderr.read())

    async def succeeded(request_id: str) -> bool:
        nonlocal scanned
        while True:
            end = stdout.rfind(b"\n") + 1
            for line in bytes(stdout[scanned:end]).splitlines():
                try:
                    response = json.loads(line).get("response")
                except (ValueError, AttributeError):
                    continue
                if isinstance(response, dict) and response.get("request_id") == request_id:
                    return response.get("subtype") == "success"
            scanned = end
            chunk = await reader.read(65536)
            if not chunk:
                return False
            stdout.extend(chunk)

    try:
        try:
            for frame in frames:
                process.stdin.write(json.dumps(frame).encode() + b"\n")
                await process.stdin.drain()
                if frame.get("request", {}).get("subtype") != "end_session" and not await succeeded(frame["request_id"]):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass  # the CLI exited first; its output says why
        process.stdin.close()
        stdout.extend(await reader.read())
        await process.wait()
        return bytes(stdout), await stderr
    finally:
        stderr.cancel()
