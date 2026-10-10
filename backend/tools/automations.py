"""Actor-bound bounded automation schedule tools."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
from typing import Any

from automation import schedules
from infra.database import get_db
from infra.db_async import run_in_db
from tools.base import Tool


def _require_context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context as require_context

    return require_context(kwargs)


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _operate(thread_id, actor_row, operation: str, params: dict):
    if operation == "schedule":
        scope_id = actor_row["project_id"] or "__workspace__"
        return {
            "schedule": asdict(
                schedules.create(
                    thread_id,
                    scope_id,
                    thread_id,
                    kind=params["kind"],
                    spec=params["spec"],
                    max_runs=params["max_runs"],
                    payload=params.get("payload"),
                    now=datetime.now(timezone.utc).isoformat(),
                    db=None,
                )
            )
        }
    if operation == "cancel":
        return {
            "cancelled": schedules.cancel(
                params["schedule_id"],
                thread_id=thread_id,
                actor_id=thread_id,
                db=None,
            )
        }
    return {
        "schedules": [
            asdict(item)
            for item in schedules.list_for_actor(
                thread_id=thread_id,
                actor_id=thread_id,
                db=get_db(),
            )
        ]
    }


async def _execute(kwargs: dict[str, Any], operation: str) -> str:
    context = _require_context(kwargs)
    if isinstance(context, str):
        return context
    thread_id, actor_row = context
    params = {
        key: value
        for key, value in kwargs.items()
        if not key.startswith("_")
    }
    try:
        result = await run_in_db(
            partial(_operate, thread_id, actor_row, operation, params)
        )
        return json.dumps(result, sort_keys=True)
    except ValueError as exc:
        return f"Error: {exc}"


class AutomationScheduleTool(Tool):
    @property
    def name(self) -> str:
        return "automation_schedule"

    @property
    def description(self) -> str:
        return (
            "Schedule a bounded clock or cadence wake for the current thread. "
            "max_runs is mandatory; clock schedules require max_runs=1."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "kind": {"type": "string", "enum": ["clock", "cadence"]},
                "spec": {
                    "type": "string",
                    "description": (
                        "ISO-8601 future time for clock, or integer seconds "
                        "for cadence."
                    ),
                },
                "max_runs": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": schedules.MAX_SCHEDULE_RUNS,
                },
                "payload": {"type": "object"},
            },
            ["kind", "spec", "max_runs"],
        )

    async def execute(self, **kwargs: Any) -> str:
        return await _execute(kwargs, "schedule")


class AutomationCancelTool(Tool):
    @property
    def name(self) -> str:
        return "automation_cancel"

    @property
    def description(self) -> str:
        return "Cancel one automation schedule owned by the current thread."

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {"schedule_id": {"type": "string", "minLength": 1}},
            ["schedule_id"],
        )

    async def execute(self, **kwargs: Any) -> str:
        return await _execute(kwargs, "cancel")


class AutomationListTool(Tool):
    @property
    def name(self) -> str:
        return "automation_list"

    @property
    def description(self) -> str:
        return "List bounded automation schedules owned by the current thread."

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema({}, [])

    async def execute(self, **kwargs: Any) -> str:
        return await _execute(kwargs, "list")
