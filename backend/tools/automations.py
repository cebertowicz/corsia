"""Actor-bound bounded automation schedule tools."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
from typing import Any

from automation import schedules, script_schedules
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


_SERIES_ITEM = _schema(
    {
        "key": {"type": "string"},
        "unit": {"type": "string"},
        "min": {"type": "number"},
        "max": {"type": "number"},
        "worse": {
            "type": "string", "enum": ["up", "down"],
            "description": "Which direction is worse; recorded but not acted on yet.",
        },
        "reduce": {
            "type": "string",
            "enum": ["last", "mean", "max"],
            "description": "How a period's readings reduce; recorded but not acted on yet (default last).",
        },
    },
    ["key"],
)


def _check_workflow(thread_id: str, workflow: Any) -> None:
    """Refuse a script schedule's workflow that is unknown or not the owner's."""
    from infra.measure_admission import workflow_refusal

    db = get_db()
    try:
        refusal = workflow_refusal(db, thread_id, str(workflow))
    finally:
        db.close()
    if refusal:
        raise ValueError(refusal)


def _script_schedule(thread_id: str, scope_id: str, params: dict) -> dict:
    """Register one model-free script run for the current thread."""
    from infra.config import WORKSPACE

    for name in ("script_path", "cadence_seconds"):
        if params.get(name) is None:
            raise ValueError(f"{name} is required for a script schedule")
    series = script_schedules.parse_series(params.get("series", []))
    if params.get("workflow") is not None:
        _check_workflow(thread_id, params["workflow"])
    path, sha256 = script_schedules.pin_script(
        str(params["script_path"]), workspace=WORKSPACE,
    )
    schedule = script_schedules.create_script(
        thread_id,
        scope_id,
        script_path=path,
        script_sha256=sha256,
        cadence_seconds=params["cadence_seconds"],
        series=series,
        timeout_seconds=params.get(
            "timeout_seconds", script_schedules.DEFAULT_TIMEOUT_SECONDS,
        ),
        workflow=params.get("workflow"),
        now=datetime.now(timezone.utc).isoformat(),
        db=None,
    )
    return {"schedule": asdict(schedule)}


def _operate(thread_id, actor_row, operation: str, params: dict):
    if operation == "schedule":
        scope_id = actor_row["project_id"] or "__workspace__"
        if params.get("kind") == script_schedules.SCRIPT_KIND:
            return _script_schedule(thread_id, scope_id, params)
        for name in ("spec", "max_runs"):
            if params.get(name) is None:
                raise ValueError(f"{name} is required for a clock or cadence schedule")
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
            "Schedule work for the current thread. kind=clock or cadence is a "
            "bounded wake of you: spec and max_runs are mandatory, and clock "
            "requires max_runs=1. kind=script runs a file you wrote on a "
            "cadence with no model turn, in your own sandbox: use it to watch "
            "a number (a count, a duration, a cost) cheaply. The script prints "
            "one JSON object per line, {\"series\": \"<key>\", \"value\": "
            "<number>}, for each series you registered, and exits 0. It may also "
            "print measure lines for events declared in a measures.yaml you "
            "synced with measure_sync: {\"measure\": {\"event.name\": \"<event>\", "
            "\"attributes\": {<name>: <value>}, \"time\": \"<ISO 8601, optional>\"}}; "
            "measure_status shows what was recorded or refused and why. You are "
            "woken once per series per day when a reading leaves its min/max, "
            "the script fails or times out, or the file changes after "
            "registration; readings inside bounds reach you as a passive "
            "update. A script schedule has no expiry: cancel it with "
            "automation_cancel. Check its last_status with automation_list."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema(
            {
                "kind": {
                    "type": "string", "enum": ["clock", "cadence", "script"],
                },
                "spec": {
                    "type": "string",
                    "description": (
                        "ISO-8601 future time for clock, or integer seconds "
                        "for cadence. Not used by script."
                    ),
                },
                "max_runs": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": schedules.MAX_SCHEDULE_RUNS,
                    "description": "Required for clock and cadence; not used by script.",
                },
                "payload": {"type": "object"},
                "script_path": {
                    "type": "string",
                    "description": (
                        "script kind: a file you wrote (.py, .sh or .js, or an "
                        "executable). Its sha256 is pinned at registration."
                    ),
                },
                "cadence_seconds": {
                    "type": "integer",
                    "minimum": script_schedules.MIN_CADENCE_SECONDS,
                    "maximum": schedules.MAX_CADENCE_SECONDS,
                    "description": "script kind: seconds between runs, at least 300.",
                },
                "series": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": script_schedules.MAX_SERIES,
                    "description": (
                        "script kind: the readings the script may print. min "
                        "and max are your own bounds; a reading outside them wakes you; a series with no bounds never wakes you."
                    ),
                    "items": _SERIES_ITEM,
                },
                "workflow": {
                    "type": "string",
                    "description": (
                        "script kind: optional. The `workflow:` value of a measures.yaml you synced; "
                        "that file's sibling SKILL.md frontmatter must name you as "
                        "`corsia: {workflow: {owner: <your thread id>}}`. Each accepted measure line "
                        "is recorded with this workflow_ref. A refusal lists the workflows you own. "
                        "Leave it out for agent-level measures."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": script_schedules.MAX_TIMEOUT_SECONDS,
                    "description": "script kind: stop a run after this long. Default 60.",
                },
            },
            ["kind"],
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
        return (
            "List automation schedules owned by the current thread. A script "
            "schedule shows state, last_status (ok, breach, no_data, error, "
            "timeout, hash_mismatch, interrupted), last_error and last_run_at; its "
            "payload shows last_measures (measure lines recorded and refused) and "
            "workflow_stale (true when its workflow is no longer yours)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _schema({}, [])

    async def execute(self, **kwargs: Any) -> str:
        return await _execute(kwargs, "list")
