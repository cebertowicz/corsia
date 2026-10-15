"""Roster update tool for the CORSIA agent and project conductors; no API token is exposed to the model."""

from __future__ import annotations

import json
from typing import Any

from core.agent_roster_mutations import (
    RosterChangeError,
    change_agent,
    require_designated_conductor_target,
)
from infra.db_async import run_in_db
from tools.base import Tool


def _actor_user_id(db, actor_thread_id: str) -> str:
    actor = db.execute(
        "SELECT user_id FROM threads WHERE id=?", (actor_thread_id,),
    ).fetchone()
    if actor is None:
        raise RosterChangeError("Current thread no longer exists")
    return actor["user_id"]


class AgentRosterEntryTool(Tool):
    """Read any thread's roster entry and revision in the caller's coordination scope."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "agent_roster_entry"

    @property
    def description(self) -> str:
        return (
            "Read the current roster fields and roster-only updated_at revision for one "
            "thread in your coordination scope (the CORSIA agent's workspace or a project conductor's project), including an "
            "inactive or not-yet-promoted thread. Use before agent_roster_update."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "required": ["thread_id"],
            "properties": {"thread_id": {"type": "string", "minLength": 1}},
        }

    async def execute(self, thread_id: str, **kwargs: Any) -> str:
        actor_thread_id = kwargs.get("_thread_id")
        if not isinstance(actor_thread_id, str) or not actor_thread_id:
            return "Error: current thread identity is unavailable."
        from infra.database import get_db

        def _read() -> dict[str, Any]:
            db = get_db()
            try:
                user_id = _actor_user_id(db, actor_thread_id)
                target = require_designated_conductor_target(
                    thread_id,
                    user_id=user_id,
                    actor_thread_id=actor_thread_id,
                    db=db,
                )
                return {
                    "thread_id": thread_id,
                    "updated_at": target["agent_roster_updated_at"],
                    "agent_enabled": bool(target["agent_enabled"]),
                    "active": target["agent_status"] == "active",
                    "responsibility": target["agent_responsibility"],
                    "responsibility_paths": json.loads(target["agent_paths"] or "[]"),
                    "responsibility_domains": json.loads(target["agent_domains"] or "[]"),
                    "coordination_notes": target["agent_notes"],
                }
            finally:
                db.close()

        try:
            return json.dumps(await run_in_db(_read), sort_keys=True)
        except RosterChangeError as exc:
            return f"Error: {exc}. Check the target thread and coordination designation."


class AgentRosterUpdateTool(Tool):
    """Edit an agent in the caller's coordination scope."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "agent_roster_update"

    @property
    def description(self) -> str:
        return (
            "Update, promote, deactivate, or demote a thread in the current "
            "coordination scope (the CORSIA agent's workspace or a project conductor's project). Server-side scope and designation "
            "checks run with the roster write; no raw API credential is needed. "
            "Omitted fields retain their current values. Returns whether the "
            "roster changed. Use only for a roster change the user authorized."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["thread_id", "expected_updated_at"],
            "properties": {
                "thread_id": {"type": "string", "minLength": 1},
                "expected_updated_at": {
                    "type": "string", "minLength": 1,
                    "description": "The target's roster updated_at from a fresh agent_roster_entry read.",
                },
                "responsibility": {"type": "string"},
                "responsibility_paths": {
                    "type": "array", "items": {"type": "string"},
                },
                "responsibility_domains": {
                    "type": "array", "items": {"type": "string"},
                },
                "coordination_notes": {"type": ["string", "null"]},
                "agent_enabled": {"type": "boolean"},
                "active": {"type": "boolean"},
            },
        }

    async def execute(
        self, thread_id: str, expected_updated_at: str, **kwargs: Any,
    ) -> str:
        actor_thread_id = kwargs.get("_thread_id")
        if not isinstance(actor_thread_id, str) or not actor_thread_id:
            return "Error: current thread identity is unavailable; retry from a live CORSIA agent or conductor turn."
        from infra.database import get_db

        def _change() -> dict[str, Any]:
            db = get_db()
            try:
                user_id = _actor_user_id(db, actor_thread_id)
            finally:
                db.close()
            body = {key: value for key, value in kwargs.items() if not key.startswith("_")}
            return change_agent(
                thread_id, body, user_id=user_id, actor_thread_id=actor_thread_id,
                expected_updated_at=expected_updated_at,
            )

        try:
            result = await run_in_db(_change)
        except RosterChangeError as exc:
            return f"Error: {exc}. Check the current coordination designation, target scope, and roster fields."
        return json.dumps(result, sort_keys=True)
