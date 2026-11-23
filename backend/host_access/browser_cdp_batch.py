"""A CDP client that can also send a batch of commands and read every reply in one pass."""
from __future__ import annotations

import json

from host_access.browser_cdp import CdpClient


class BatchingCdpClient(CdpClient):
    """CdpClient plus call_many, for a reader that asks the same question of many nodes."""

    async def call_many(self, session_id: str | None, method: str, params: list[dict]) -> list[dict | None]:
        """Send `method` once per entry of `params`, then read every reply in one pass; None where one failed.

        Chrome answers in order on one connection, so a batch costs about one round trip rather than one per
        command. Events read meanwhile are handled exactly as call_in handles them.
        """
        pending: dict[int, int] = {}
        for index, one in enumerate(params):
            pending[await self.send(method, session_id=session_id, **one)] = index
        results: list[dict | None] = [None] * len(params)
        while pending:
            response = json.loads(await self.socket.recv())
            reply_id = response.get("id")
            if not isinstance(reply_id, int) or reply_id not in pending:
                self._event(response)
                continue
            index = pending.pop(reply_id)
            result = response.get("result")
            results[index] = result if "error" not in response and isinstance(result, dict) else None
        return results
