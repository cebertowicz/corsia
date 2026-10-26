"""Observe rules: the rows that register the artifact a tool call created to its caller.

An ``observe`` row in ``event_rules`` names a tool, for a shell ``bash`` plus the command's
leading arguments, and the artifact type (``item_type``) the connector's extractor reads from
the result. It has no recipient: it always registers to the caller, and only when the artifact
has no live owner. Otherwise the caller becomes at most a passive subscriber (the registry's
rule, never re-derived here).

Who may write: an agent's row applies to its own calls in its own project, and a shipped
rule set's row (``scope: global``, written by the release) applies to every caller. Nobody
writes a row that registers to someone else. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from events import connectors, rules
from events.rule_actions import RuleRefused
from events.rule_outcomes import Outcome, RuleEvent
from infra.database import write_transaction

OBSERVE = "observe"
SHIPPED_SCOPE = "global"
SHELL_TOOLS = frozenset({"bash"})
MAX_PREFIX = 8
MAX_ARGUMENT = 100
RULE_SET_ORIGIN = "rule_set:"
_ROUTE_ONLY = ("event_type", "match", "action", "to", "mode", "note", "script_id", "workflow_id",
               "request_identity_fact")


@dataclass(frozen=True)
class ObserveRule:
    id: str
    scope: str
    connector: str
    tool: str
    argv_prefix: tuple[str, ...]
    item_type: str
    origin: str
    created_by: str
    enabled: bool = True
    command_arg: str = "command"  # the argument of a shell tool that holds the command


def from_row(row: Mapping[str, Any]) -> ObserveRule:
    return ObserveRule(
        id=row["id"], scope=row["scope"], connector=row["connector"], tool=row["tool"],
        argv_prefix=tuple(json.loads(row["argv_prefix"] or "[]")), item_type=row["item_type"],
        origin=row["origin"], created_by=row["created_by"], enabled=bool(row["enabled"]),
    )


def shape(rule: ObserveRule) -> dict[str, Any]:
    return {
        "id": rule.id, "kind": OBSERVE, "enabled": rule.enabled, "scope": rule.scope,
        "connector": rule.connector, "tool": rule.tool, "argv_prefix": list(rule.argv_prefix),
        "item_type": rule.item_type, "origin": rule.origin, "written_by": rule.created_by,
    }


def applicable(db: sqlite3.Connection, thread_id: str, tool: str) -> list[ObserveRule]:
    """The enabled observe rows that cover ``thread_id``'s call of ``tool``: a shipped rule set's
    and the thread's own in its project. One read through the partial index on observe rows."""
    return [from_row(row) for row in db.execute(
        "SELECT * FROM event_rules WHERE kind='observe' AND enabled=1 AND tool=? "
        "AND (scope=? OR scope=? || (SELECT project_id FROM threads WHERE id=?)) "
        "AND (origin LIKE ? || '%' OR created_by=?) ORDER BY created_at, id",
        (tool, SHIPPED_SCOPE, rules.PROJECT_PREFIX, thread_id, RULE_SET_ORIGIN, thread_id),
    ).fetchall()]


def _known_types() -> list[str]:
    return sorted({item_type for connector in connectors.registered_connectors()
                   for item_type in connectors.get_manifest(connector).artifact_extractors})


def _connector(item_type: Any, connector: Any) -> str:
    """The connector whose extractor reads ``item_type``, or the reason none does."""
    if not isinstance(item_type, str) or not item_type:
        raise RuleRefused(f"an observe rule needs an item_type; the types with an extractor are {_known_types()}")
    declared = connectors.extractor_connectors(item_type)
    if connector is not None and connector not in declared:
        declared = ()
    if not declared:
        raise RuleRefused(
            f"no connector reports on {item_type!r}, so registering it would achieve nothing; "
            f"the types with an extractor are {_known_types()} (`types` lists them)")
    if len(declared) > 1:
        raise RuleRefused(f"{item_type!r} is read by {list(declared)}; name one as connector")
    return declared[0]


def _tool(tool: Any) -> str:
    from tools.catalog import normalize_tool_name

    name = normalize_tool_name(tool if isinstance(tool, str) else None)
    if name not in SHELL_TOOLS:
        raise RuleRefused(
            f"{tool!r} can't be observed yet: observing a connected app's tool needs the app's own "
            f"stamp on its results, which isn't built. An observe rule covers shell commands: "
            f"tool {sorted(SHELL_TOOLS)[0]!r} with an argv_prefix such as ['gh', 'pr', 'create'].")
    return name


def _prefix(raw: Any) -> list[str]:
    if (not isinstance(raw, list) or not 0 < len(raw) <= MAX_PREFIX
            or not all(isinstance(arg, str) and 0 < len(arg) <= MAX_ARGUMENT and "\n" not in arg
                       for arg in raw)):
        raise RuleRefused(f"argv_prefix is the command's leading arguments: a list of 1 to {MAX_PREFIX} "
                          f"words, such as ['gh', 'pr', 'create']")
    return list(raw)


def create(
    db: sqlite3.Connection, *, writer: Mapping[str, Any], scope: str, now: str,
    tool: Any, argv_prefix: Any, item_type: Any, connector: Any, strays: Mapping[str, Any],
) -> dict[str, Any]:
    """Write one agent observe row within the writer limits, or raise :class:`RuleRefused`.

    ``writer`` is the caller's thread row, already checked to be a project agent;
    ``strays`` are the route-only fields the caller passed, which an observe row refuses.
    """
    named = sorted(name for name in _ROUTE_ONLY if strays.get(name) is not None)
    if named:
        raise RuleRefused(f"An observe rule takes tool, argv_prefix and item_type and always registers "
                          f"to you; it takes no {', '.join(named)}.")
    name, words, owner = _tool(tool), _prefix(argv_prefix), _connector(item_type, connector)
    if db.execute(
        "SELECT 1 FROM event_rules WHERE kind='observe' AND enabled=1 AND created_by=? AND scope=? "
        "AND tool=? AND argv_prefix=? AND item_type=?",
        (writer["id"], scope, name, json.dumps(words), item_type),
    ).fetchone():
        raise RuleRefused("You already have that observe rule; `list` shows it.")
    rule_id = "rule_" + uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO event_rules (id, created_at, updated_at, enabled, kind, scope, connector, origin, "
        "created_by, authority_ref, tool, argv_prefix, item_type) VALUES (?,?,?,1,'observe',?,?,?,?,'self',?,?,?)",
        (rule_id, now, now, scope, owner, f"agent:{writer['id']}", writer["id"], name,
         json.dumps(words), item_type),
    )
    rules.prune_disabled(db, scope, now)
    return shape(from_row(db.execute("SELECT * FROM event_rules WHERE id=?", (rule_id,)).fetchone()))


def check_disable(rule: ObserveRule, caller: str) -> None:
    """Only an observe row's writer turns it off; a shipped rule set's row applies to everyone."""
    if rule.origin.startswith(RULE_SET_ORIGIN):
        raise RuleRefused("A shipped rule set's rule applies to every agent in the project; "
                          "only the person turns it off.")
    if caller != rule.created_by:
        raise RuleRefused("Only the rule's writer can turn off an observe rule: it registers to its writer.")


def record(
    thread_id: str, rule: ObserveRule, outcome: str, artifact: Mapping[str, Any] | None, reason: str | None,
) -> None:
    """One observe outcome (``registered`` or ``not_registered``) in the rule outcome record, which
    ``explain`` reads. Never fails the call."""
    ref = {"integration": artifact["integration"], "type": artifact["type"],
           "external_id": artifact["external_id"]} if artifact else None
    event = RuleEvent(
        event_id="observe_" + uuid.uuid4().hex[:12], connector=rule.connector,
        event_type=f"observe:{rule.tool}", watch_event="", facts={}, artifact_ref=ref)
    try:
        with write_transaction(None, scope="observe_rule_outcome") as db:
            rules.record(db, event, [Outcome(rule.id, thread_id, outcome, reason=reason)])
    except Exception:
        logger.exception("observe rule {} outcome was not recorded", rule.id)
