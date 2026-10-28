"""Decode and acknowledge the browser producer's local protocol."""
from __future__ import annotations

import asyncio
import json

from host_access import browser_identity as identity


def json_object(raw: str) -> dict:
    """Decode exactly one object; partial or noisy output is not protocol evidence."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


async def close_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request_id: str) -> None:
    """Consume the producer acknowledgement; transport EOF is not success."""
    try:
        writer.write((json.dumps({"id": request_id, "action": "close"}) + "\n").encode())
        await writer.drain()
        response = json_object((await reader.readline()).decode("utf-8"))
        data = response.get("data")
        if (response.get("id") != request_id or response.get("success") is not True
                or not isinstance(data, dict) or data.get("closed") is not True):
            raise identity.BrowserIdentityError("browser producer did not acknowledge close")
    finally:
        writer.close()
        await writer.wait_closed()
