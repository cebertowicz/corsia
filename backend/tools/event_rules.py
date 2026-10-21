"""The ``event_rules`` agent tool: write, turn off, list, test and explain event rules."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from events import rule_authoring
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from tools.base import Tool

_OPERATIONS = ("create", "disable", "list", "test", "explain")


def _context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context
    return _require_context(kwargs)


def _run(operation: str, thread_id: str, project_id: str | None, kwargs: dict[str, Any]) -> Any:
    now = datetime.now(timezone.utc).isoformat()
    if operation in ("create", "disable"):
        with write_transaction(None, scope="event_rules_tool") as db:
            if operation == "create":
                return rule_authoring.create(
                    db, writer=thread_id, project_id=project_id,
                    connector=kwargs.get("connector"), event_type=kwargs.get("event_type"),
                    match=kwargs.get("match"), action=kwargs.get("action", "deliver"),
                    to=kwargs.get("to"), mode=kwargs.get("mode"), note=kwargs.get("note"),
                    scope=kwargs.get("scope"), now=now,
                )
            return rule_authoring.disable(
                db, caller=thread_id, project_id=project_id, rule_id=kwargs.get("rule_id"), now=now)
    db = get_db()
    try:
        if operation == "list":
            return {"rules": rule_authoring.list_rules(db, project_id=project_id, now=now)}
        if operation == "test":
            return {"matches": rule_authoring.dry_run(
                db, project_id=project_id, connector=kwargs.get("connector"),
                event_type=kwargs.get("event_type"), facts=kwargs.get("facts"))}
        return rule_authoring.explain(
            db, project_id=project_id, event_id=kwargs.get("event_id"),
            artifact=kwargs.get("artifact"), thread=kwargs.get("thread"))
    finally:
        db.close()


class EventRulesTool(Tool):
    @property
    def name(self) -> str:
        return "event_rules"

    @property
    def description(self) -> str:
        return (
            "Rules that act on events from connected apps (a pull request merging, a check "
            "finishing, an email arriving), on top of the routing that already wakes a file's "
            "owner. create: `deliver` wakes you (or puts the event in your digest) when an event "
            "of `connector`/`event_type` matches up to 4 `match` tests (op eq, not_eq, in, prefix, "
            "present) on that type's declared fields; your `note` (500 characters) comes with the "
            "wake, so write what to do then. `drop` stops deliveries of matching events to you. "
            "Your rules cover your own project and deliver only to you (`to: self`, the default) or "
            "to the event's file's registered owner (`to: registered_owner`). A rule wakes a chat "
            "at most once an hour; more go to its digest. A paused chat gets a digest instead. "
            "disable: turn off a rule you wrote or receive. list: the project's rules, with when "
            "each last acted and its wakes in the last 24 hours. test: which rules a set of `facts` "
            "would match. explain: why an event (`event_id`, or `artifact`, the file's external id "
            "such as owner/repo#7) did or didn't reach a chat through a rule."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": list(_OPERATIONS)},
                "connector": {"type": "string", "description": "create/test: the connected app, e.g. github"},
                "event_type": {"type": "string", "description": "create/test: a declared type, e.g. pr.merged"},
                "match": {
                    "type": "array", "maxItems": 4,
                    "items": {"type": "object", "properties": {
                        "field": {"type": "string"},
                        "op": {"type": "string", "enum": ["eq", "not_eq", "in", "prefix", "present"]},
                        "value": {},
                    }, "required": ["field"]},
                    "description": "create: tests that must all pass",
                },
                "action": {"type": "string", "enum": ["deliver", "drop"]},
                "to": {"type": "string",
                       "description": "create: self (default) or registered_owner; a conductor may "
                                      "name thread:<agent id> for a digest"},
                "mode": {"type": "string", "enum": ["wake", "digest"]},
                "note": {"type": "string", "maxLength": 500},
                "scope": {"type": "string", "description": "create: project:<id>; only your own"},
                "rule_id": {"type": "string", "description": "disable: the rule to turn off"},
                "facts": {"type": "object", "description": "test: the event's fields"},
                "event_id": {"type": "string", "description": "explain: the event's id"},
                "artifact": {"type": "string", "description": "explain: the file's external id"},
                "thread": {"type": "string", "description": "explain: only this chat"},
            },
            "required": ["operation"],
        }

    async def execute(self, **kwargs: Any) -> str:
        context = _context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor_row = context
        operation = kwargs.get("operation")
        if operation not in _OPERATIONS:
            return f"Error: operation is one of {', '.join(_OPERATIONS)}"
        try:
            result = await run_in_db(_run, operation, thread_id, actor_row.get("project_id"), kwargs)
        except rule_authoring.RuleRefused as exc:
            return f"Refused: {exc}"
        return json.dumps(result, sort_keys=True)
