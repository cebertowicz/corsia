"""Agent-facing opt-in registration for one exact restart-controlled Turn."""

from __future__ import annotations

import json
from typing import Any

from tools.base import Tool


class RestartResumeTool(Tool):
    @property
    def name(self) -> str:
        return "restart_resume"

    @property
    def description(self) -> str:
        return (
            "Register the current exact Turn to resume after the pending system "
            "restart. Call this only when unfinished work must continue."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        from automation.restart_wakes import register_restart_resume
        from infra.db_async import run_in_db

        thread_id = kwargs.get("_thread_id")
        identity = kwargs.get("_host_access_turn_identity")
        if not isinstance(thread_id, str) or identity is None:
            return "Error: restart_resume requires the current exact active Turn."
        registration_id = await run_in_db(
            register_restart_resume,
            thread_id,
            identity.turn_id,
        )
        return json.dumps({
            "ok": True,
            "status": "registered",
            "registration_id": registration_id,
            "turn_id": identity.turn_id,
        })
