"""A CDP client that can also send a batch of commands and read every reply in one pass."""
from __future__ import annotations

import json
import logging

from host_access.browser_cdp import CdpClient

log = logging.getLogger(__name__)


class BatchingCdpClient(CdpClient):
    """CdpClient plus call_many, for a reader that asks the same question of many nodes."""

    async def call_many(self, session_id: str | None, method: str, params: list[dict]) -> list[dict | None]:
        """Send `method` once per entry of `params`, then read every reply in one pass; None where one failed.

        Chrome answers in order on one connection, so a batch costs about one round trip rather than one per
        command. Events read meanwhile are handled exactly as call_in handles them, and a failed entry is
        logged as call_in logs one; it does not fail the batch, since one node's error says nothing about
        the others.
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
            error, result = response.get("error"), response.get("result")
            if error is not None or not isinstance(result, dict):
                code = error.get("code") if isinstance(error, dict) else None
                # Method, id and numeric code only, as call_in: an error's message can quote the request's params.
                log.debug("CDP %s (id %s) failed with code %s", method, reply_id, code if isinstance(code, int) else None)
                continue
            results[index] = result
        return results
