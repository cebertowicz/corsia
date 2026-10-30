"""The ``event_rules`` agent tool: write, turn off, list, test and explain event rules, and emit an event."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from events import observe_rules, rule_actions, rule_authoring, rule_listing, rule_runs, rule_types, rules
from infra import config
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from tools.base import Tool

logger = logging.getLogger(__name__)

_OPERATIONS = ("create", "disable", "list", "test", "explain", "types", "emit")


def _context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context
    return _require_context(kwargs)


def _run(operation: str, thread_id: str, project_id: str | None, kwargs: dict[str, Any]) -> Any:
    now = datetime.now(timezone.utc).isoformat()
    if operation in ("create", "disable"):
        # Resolving the project's repositories runs git: never under the write lock.
        tracked = (rule_authoring.tracked_repositories(
            kwargs.get("connector"), kwargs.get("match"), project_id) if operation == "create" else None)
        workflows = (_workflows(thread_id, project_id)
                     if operation == "create" and kwargs.get("action") == rules.START_WORKER else None)
        with write_transaction(None, scope="event_rules_tool") as db:
            if operation == "create":
                result = rule_authoring.create(
                    db, writer=thread_id, project_id=project_id,
                    connector=kwargs.get("connector"), event_type=kwargs.get("event_type"),
                    match=kwargs.get("match"), action=kwargs.get("action"),
                    to=kwargs.get("to"), mode=kwargs.get("mode"), note=kwargs.get("note"),
                    scope=kwargs.get("scope"), now=now, tracked=tracked,
                    script_id=kwargs.get("script_id"), workflow_id=kwargs.get("workflow_id"),
                    request_identity_fact=kwargs.get("request_identity_fact"), workflows=workflows,
                    kind=kwargs.get("kind"), tool=kwargs.get("tool"), argv_prefix=kwargs.get("argv_prefix"),
                    item_type=kwargs.get("item_type"),
                )
            else:
                result = rule_authoring.disable(
                    db, caller=thread_id, project_id=project_id, rule_id=kwargs.get("rule_id"), now=now)
        emit_product_event(
            "event_rule", "created" if operation == "create" else "disabled", thread_id=thread_id,
            properties={"rule_id": result["id"], "kind": result["kind"], "scope": project_id},
        )
        return result
    db = get_db()
    try:
        if operation == "types":
            return rule_types.types(db, kwargs.get("connector"))
        if operation == "list":
            return {"rules": rule_listing.list_rules(db, project_id=project_id, now=now),
                    "rule_sets": rule_listing.rule_set_rows(db, now=now, project_id=project_id)}
        if operation == "test":
            return {"matches": rule_authoring.dry_run(
                db, caller=thread_id, project_id=project_id, connector=kwargs.get("connector"),
                event_type=kwargs.get("event_type"), facts=kwargs.get("facts"))}
        return rule_authoring.explain(
            db, project_id=project_id, event_id=kwargs.get("event_id"),
            artifact=kwargs.get("artifact"), thread=kwargs.get("thread"),
            event_type=kwargs.get("event_type"))
    finally:
        db.close()


def _workflows(thread_id: str, project_id: str | None) -> dict[str, Any]:
    # Reads skill folders: never under the write lock.
    db = get_db()
    try:
        return rule_actions.reachable_workflows(db, thread_id, project_id, config.WORKSPACE)
    finally:
        db.close()


async def _emit(thread_id: str, kwargs: dict[str, Any]) -> str:
    """Send one declared event as this chat; the receipt, or the refusal with its fix."""
    from events.intake import admit_event, emitted_event_id
    from events.intake_errors import failure_code, format_event_error

    event_type = kwargs.get("event_type")
    if not isinstance(event_type, str) or not event_type:
        return "Error: emit needs event_type, the declared name such as qa.review_completed."
    event_id = emitted_event_id(event_type, kwargs.get("id"))
    try:
        result = await admit_event(
            event_type=event_type, facts=kwargs.get("facts"), origin="agent",
            producer=thread_id, event_id=event_id,
        )
    except Exception as exc:  # noqa: BLE001 - unreachable (retry with the same id) or a defect (report it)
        logger.exception("event_rules emit failed inside the intake")
        if failure_code(exc) == "internal_error":
            return f"Error: {format_event_error('internal_error', 'the event intake failed unexpectedly')}"
        return f"Error: {format_event_error('intake_unreachable', 'event intake is unavailable')}"
    if result.refusal is None:
        return json.dumps({"event_id": event_id, "duplicate": result.duplicate,
                           "rules_matched": result.rules_matched}, ensure_ascii=False)
    return f"Error: {format_event_error(result.refusal.code, result.refusal.text)}"


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
            f"of `connector`/`event_type` matches up to {rules.MAX_TESTS} `match` tests (op "
            f"{', '.join(rules.OPERATORS)}) on that type's declared fields; your `note` "
            f"({rules.NOTE_LIMIT} characters) comes with the wake, so write what to do then. `drop` stops deliveries of matching events to you. "
            "`run_script` runs one of your script schedules (`script_id`, from automation_list; "
            "register it with automation_schedule without cadence_seconds so only rules start it) "
            "with the event as JSON on its stdin, within about a minute; it takes no `to`, `mode` "
            "or `note`, and the event still reaches whoever it reaches today. For GitHub reads, "
            "use `corsia github read <api-path>`; build the path from the rule's own fields, "
            "never from event text. An event the run "
            "emits carries the rule, so a rule already in that chain is skipped, not run again; "
            "a run while you are paused is skipped and not replayed. A script runs once at a time, "
            f"so runs wait their turn; a match while {rule_runs.QUEUE_CAP} of its rule's runs wait "
            "is skipped. "
            "`start_worker` starts one read-only worker on your model running `workflow_id` (a skill "
            "you can read whose corsia.yaml workflow declares a `result`) once per value of the "
            "event's `request_identity_fact`; the worker reads only the event's `staging_path` (in your "
            "staging folder) and the `head_repo`/`head_sha` tree, "
            "and its checked answer comes back as a worker.result event on the item (worker.no_result "
            f"when it returns nothing usable); a match while {rule_runs.QUEUE_CAP} of its rule's "
            "starts wait is skipped. "
            "Your rules cover your own project and deliver only to you (`to: self`, the default) or "
            "to the event's file's registered owner (`to: registered_owner`); that owner is woken "
            "only when it is you or one of your temporary workers, otherwise the event goes to "
            "its digest. A paused chat gets a digest instead. "
            "`kind: observe` (create) registers you to the file a shell command's or connected app's output names: "
            "for a command give `tool: bash` and the command's leading words as `argv_prefix` (such as gh pr create); "
            "for a connected app give its upstream tool name as `tool` (not its CORSIA alias); "
            "the connector that reads `item_type` names the apps it covers. Give the `item_type` a connector reads from the output (`types` lists them), "
            "and no `to`, `mode`, `note`, `match` or `action`. The result tells you what happened; "
            "a file that already has an owner is never taken from it. A shipped rule set's own "
            "observe rules are in list's `rule_sets` and cover every agent. "
            "disable: turn off a rule you wrote or receive. list: the project's rules, with when "
            "each last acted and its wakes, script and worker starts and skipped runs in the last 24 hours. "
            "types: each connector's event types with their fields, and the item types its observe rules read. test: which rules a set of `facts` "
            "would match. explain: why an event (`event_id`, or `artifact`, the file's external id "
            "such as owner/repo#7) did or didn't reach a chat through a rule, or, given `event_type`, "
            "the recent refusals of that declared type. emit: send one event of a declared "
            "`event_type` (its full name, such as qa.review_completed, declared use: route or "
            "both) with `facts` as you; `id` makes a retry safe. The receipt says how many rules "
            "matched and returns the event's `event_id`, which explain takes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": list(_OPERATIONS)},
                "connector": {"type": "string", "description": "create/test: the connected app, e.g. github"},
                "match": {
                    "type": "array", "maxItems": rules.MAX_TESTS,
                    "items": {"type": "object", "properties": {
                        "field": {"type": "string"},
                        "op": {"type": "string", "enum": list(rules.OPERATORS)},
                        "value": {},
                    }, "required": ["field"]},
                    "description": "create: tests that must all pass",
                },
                "action": {"type": "string",
                           "enum": [rules.DELIVER, rules.DROP, rules.RUN_SCRIPT, rules.START_WORKER]},
                "workflow_id": {"type": "string",
                                "description": "create with start_worker: the workflow skill's name"},
                "request_identity_fact": {
                    "type": "string",
                    "description": "create with start_worker: the event fact naming one request"},
                "script_id": {"type": "string",
                              "description": "create with run_script: your script schedule's id"},
                "to": {"type": "string",
                       "description": "create: self (default) or registered_owner; a conductor may "
                                      "name thread:<agent id> for a digest"},
                "mode": {"type": "string", "enum": [rules.WAKE, rules.DIGEST]},
                "note": {"type": "string", "maxLength": rules.NOTE_LIMIT},
                "kind": {"type": "string", "enum": [rules.ROUTE, observe_rules.OBSERVE],
                         "description": "create: route (the default) or observe"},
                "tool": {"type": "string", "description": "create observe: bash, or the connected app's upstream tool name"},
                "argv_prefix": {"type": "array", "items": {"type": "string"}, "maxItems": observe_rules.MAX_PREFIX,
                                "description": "create observe: the command's leading words, e.g. gh, pr, create"},
                "item_type": {"type": "string",
                              "description": "create observe: the artifact type a connector reads from the output"},
                "scope": {"type": "string", "description": "create: project:<id>; only your own"},
                "rule_id": {"type": "string", "description": "disable: the rule to turn off"},
                "facts": {"type": "object", "description": "test or emit: the event's fields"},
                "event_id": {"type": "string",
                             "description": "explain: the event's id, as an emit receipt returns it"},
                "id": {"type": "string", "description": "emit: a stable identity that makes a retry safe "
                       "(same as the CLI's --id)"},
                "event_type": {"type": "string", "description": "create/test: a declared type, e.g. pr.merged; "
                               "emit and explain: the full declared name, e.g. qa.review_completed"},
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
        if operation == "emit":
            return await _emit(thread_id, kwargs)
        try:
            result = await run_in_db(_run, operation, thread_id, actor_row.get("project_id"), kwargs)
        except rule_actions.RuleRefused as exc:
            return f"Refused: {exc}"
        return json.dumps(result, sort_keys=True)
