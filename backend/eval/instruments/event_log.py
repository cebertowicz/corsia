
from __future__ import annotations

import contextlib
from typing import AsyncIterator
from unittest.mock import patch


@contextlib.asynccontextmanager
async def event_log(
    *,
    thread_id: str | None = None,
) -> AsyncIterator[list[dict]]:
    captured: list[dict] = []

    from ws import turn_helpers

    real_fn = getattr(turn_helpers, "broadcast_to_thread", None)

    async def fake_broadcast(sid: str, payload: dict) -> None:
        if thread_id is None or sid == thread_id:
            captured.append(dict(payload))
        if real_fn is not None:
            await real_fn(sid, payload)

    if real_fn is None:
        yield captured
        return

    with patch.object(turn_helpers, "broadcast_to_thread", new=fake_broadcast):
        yield captured
