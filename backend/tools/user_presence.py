"""The ``user_presence`` read tool: the ``User presence`` line, on demand.

Covers the calling thread only; another thread's presence is not the agent's
to read. The result is rendered from the same state as the per-batch line, so
its ``line`` is byte-equal to ``infra.presence_view.presence_line`` at the same
``as_of``.
"""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool


class UserPresenceTool(Tool):
    """Whether the user is in this conversation now, and since the last reply."""

    @property
    def name(self) -> str:
        return "user_presence"

    @property
    def description(self) -> str:
        return (
            "Whether the user is in this conversation now, since when, and "
            "whether they have been here since your last reply. Same facts as "
            "the `User presence` line, computed at the moment of the call. "
            "Takes no arguments."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        from infra.database import get_db
        from infra.presence_view import user_presence_payload
        from infra.time import utc_now

        caller = kwargs.get("_thread_id")
        if not isinstance(caller, str) or not caller:
            return self._failed("the calling thread could not be resolved")

        def _read() -> dict | None:
            db = get_db()
            try:
                return user_presence_payload(db, caller, as_of=utc_now())
            finally:
                db.close()

        try:
            payload = await run_in_db(_read)
        except Exception as exc:
            return self._failed(type(exc).__name__)
        if payload is None:
            return self._failed("this conversation was not found")
        return json.dumps(payload)

    @staticmethod
    def _failed(cause: str) -> str:
        return (
            f"User presence could not be read just now ({cause}). Nothing "
            "changed; call user_presence again, or treat presence as unknown."
        )
