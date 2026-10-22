"""Writing and reading agent event rules: create, disable, list, test, explain.

The writer limits live here, checked when a rule is written:

- an agent's rule covers events in its own project, and delivers only to itself
  or to the registered owner of the file the event is about;
- a test naming a repository names one the writer's project tracks;
- an agent's ``drop`` removes only deliveries to itself;
- the project's conductor may also send another agent in its project a digest;
- anything else is refused with the reason and who can set it up.

No approval step. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from events import rules
from events.connectors import EventTypeDecl, get_manifest
from events.rule_match import matches
from infra.queries import thread_available_sql

_EXPLAIN_LIMIT = 50


def _prune_disabled(db: sqlite3.Connection, scope: str, now: str) -> None:
    """Keep disabled rules briefly for inspection, then discard them, and what
    they did, in their own scope."""
    cutoff = (datetime.fromisoformat(now) - rules.RETENTION).isoformat()
    stale = "SELECT id FROM event_rules WHERE scope=? AND enabled=0 AND updated_at<?"
    db.execute(f"DELETE FROM event_route_decisions WHERE rule_id IN ({stale})", (scope, cutoff))
    db.execute(f"DELETE FROM event_rules WHERE id IN ({stale})", (scope, cutoff))


class RuleRefused(ValueError):
    """A request the writer limits or the rule shape refuse; the text says why."""


def _thread(db: sqlite3.Connection, thread_id: str) -> Mapping[str, Any] | None:
    return db.execute(
        f"SELECT id, project_id, spawned_by, agent_enabled, {thread_available_sql()} AS available "
        "FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()


def _temporary(row: Mapping[str, Any]) -> bool:
    return row["spawned_by"] is not None and not row["agent_enabled"]


def _conductor(db: sqlite3.Connection, project_id: str) -> str | None:
    from infra.conductor import get_project_conductor

    return get_project_conductor(project_id, db=db)


def _decl(connector: Any, event_type: Any) -> EventTypeDecl:
    if not isinstance(connector, str) or not isinstance(event_type, str):
        raise RuleRefused("connector and event_type are required")
    try:
        return get_manifest(connector).decl(event_type)
    except KeyError as exc:
        raise RuleRefused(str(exc.args[0]) if exc.args else str(exc)) from None


def _match(raw: Any, decl: EventTypeDecl) -> list[dict[str, Any]]:
    tests = [] if raw is None else raw
    if not isinstance(tests, list) or len(tests) > rules.MAX_TESTS:
        raise RuleRefused(f"match is a list of at most {rules.MAX_TESTS} tests")
    clean = []
    for test in tests:
        if not isinstance(test, Mapping):
            raise RuleRefused("each test is an object with field, op and value")
        field, op = test.get("field"), test.get("op", "eq")
        if field not in decl.filterable_fields:
            raise RuleRefused(
                f"{decl.event_type} has no field {field!r}; use one of {list(decl.filterable_fields)}")
        if op not in rules.OPERATORS:
            raise RuleRefused(f"op must be one of {list(rules.OPERATORS)}")
        item: dict[str, Any] = {"field": field, "op": op}
        if op != "present":
            value = test.get("value")
            if value is None:
                raise RuleRefused(f"the {op} test on {field} needs a value")
            if op == "in" and not isinstance(value, list):
                raise RuleRefused(f"the in test on {field} needs a list")
            if op == "prefix" and not isinstance(value, str):
                raise RuleRefused(f"the prefix test on {field} needs text")
            item["value"] = value
        clean.append(item)
    return clean


_UNREADABLE = "This project's repositories can't be read right now; try again."


def tracked_repositories(connector: Any, match: Any, project_id: str | None) -> frozenset[str] | None:
    """The project's repositories when a test names the event's repository, else ``None``.

    Resolving them runs git, so call this before opening the write transaction.
    """
    try:
        manifest = get_manifest(connector) if isinstance(connector, str) else None
    except KeyError:
        return None  # create refuses the connector
    if manifest is None or manifest.project_repositories is None or not project_id:
        return None
    if not any(isinstance(test, Mapping) and test.get("field") == manifest.repository_fact
               for test in (match if isinstance(match, list) else ())):
        return None
    tracked = manifest.project_repositories(project_id)
    if tracked is None:
        raise RuleRefused(_UNREADABLE)
    return tracked


def _repositories(connector: str, tests: list[dict[str, Any]], tracked: frozenset[str] | None) -> None:
    """A test naming the event's repository must name one this project tracks."""
    manifest = get_manifest(connector)
    fact = manifest.repository_fact if manifest.project_repositories else None
    named = [value for test in tests if test["field"] == fact and test["op"] in rules.NAMING
             for value in (test["value"] if test["op"] == "in" else [test["value"]])]
    if not named:
        return
    if tracked is None:
        raise RuleRefused(_UNREADABLE)  # fails closed when no list was read for this create
    known = {repo.casefold() for repo in tracked}
    for value in named:
        if str(value).casefold() not in known:
            raise RuleRefused(
                f"{value} isn't one of this project's repositories "
                f"({', '.join(sorted(tracked)) or 'it tracks none'}).")


def _who_sets_it_up(db: sqlite3.Connection, project_id: str) -> str:
    conductor = _conductor(db, project_id)
    if conductor:
        return f"Ask the project's conductor ({conductor}) to set up a digest for that agent."
    return "This project has no conductor; ask the person to set it up."


def _recipient(
    db: sqlite3.Connection, writer: str, project_id: str, to: Any, action: str, mode: str | None,
) -> tuple[str, str]:
    """The stored ``to`` and the writer limit that allows it."""
    if to in (None, "self", rules.THREAD_PREFIX + writer):
        return rules.THREAD_PREFIX + writer, "self"
    if action == rules.DROP:
        raise RuleRefused("A drop removes only deliveries to you; leave `to` out or set it to self.")
    if to == rules.REGISTERED_OWNER:
        return to, "registered_owner"
    target = rules.thread_target(to) if isinstance(to, str) else None
    if not target:
        raise RuleRefused("to is self, registered_owner or thread:<agent id>")
    if _conductor(db, project_id) != writer:
        raise RuleRefused(
            "Your rules deliver only to you or to the registered owner of the file the event "
            f"is about. {_who_sets_it_up(db, project_id)}")
    row = _thread(db, target)
    if row is None or row["project_id"] != project_id or not row["available"]:
        raise RuleRefused(f"{target} is not an agent in this project")
    if _temporary(row):
        raise RuleRefused("A temporary agent can't receive rules; it ends with its task.")
    if mode != rules.DIGEST:
        raise RuleRefused("A conductor's rule for another agent delivers as a digest only.")
    return rules.THREAD_PREFIX + target, "conductor"


def create(
    db: sqlite3.Connection, *, writer: str, project_id: str | None, connector: Any,
    event_type: Any, match: Any = None, action: Any = rules.DELIVER, to: Any = None,
    mode: Any = None, note: Any = None, scope: Any = None, now: str,
    tracked: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Write one rule within the writer limits, or raise :class:`RuleRefused`.

    ``tracked`` is :func:`tracked_repositories`, read before the transaction.
    """
    row = _thread(db, writer)
    if row is None:
        raise RuleRefused("this chat was not found")
    if _temporary(row):
        raise RuleRefused("A temporary agent can't write rules; they would outlive its task. "
                          "Ask the agent that started you.")
    if not project_id:
        raise RuleRefused("Rules cover a project's events; this chat is in no project.")
    own_scope = rules.project_scope(project_id)
    if scope is not None and scope != own_scope:
        raise RuleRefused(
            f"You can only write rules about events you can already read: those in your own "
            f"project ({own_scope}). {_who_sets_it_up(db, project_id)}")
    decl = _decl(connector, event_type)
    tests = _match(match, decl)
    _repositories(connector, tests, tracked)
    if action not in (rules.DELIVER, rules.DROP):
        raise RuleRefused("action is deliver or drop")
    if action == rules.DROP:
        if mode is not None or note is not None:
            raise RuleRefused("A drop takes no mode or note.")
    else:
        mode = rules.WAKE if mode is None else mode
        if mode not in (rules.WAKE, rules.DIGEST):
            raise RuleRefused("mode is wake or digest")
        if note is not None and (not isinstance(note, str) or len(note) > rules.NOTE_LIMIT):
            raise RuleRefused(f"note is text of at most {rules.NOTE_LIMIT} characters")
    stored_to, authority = _recipient(db, writer, project_id, to, action, mode)
    rule_id = "rule_" + uuid.uuid4().hex[:12]
    db.execute(
        'INSERT INTO event_rules (id, created_at, updated_at, enabled, scope, connector, '
        'event_type, "match", action, "to", mode, note, origin, created_by, authority_ref) '
        "VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)",
        (rule_id, now, now, own_scope, connector, event_type, json.dumps(tests), action,
         stored_to, mode, note, f"agent:{writer}", writer, authority),
    )
    _prune_disabled(db, own_scope, now)
    return _shape(rules.from_row(db.execute("SELECT * FROM event_rules WHERE id=?", (rule_id,)).fetchone()))


def disable(
    db: sqlite3.Connection, *, caller: str, project_id: str | None, rule_id: Any, now: str,
) -> dict[str, Any]:
    """Turn one rule off: its writer, its recipient or the project's conductor may."""
    row = db.execute("SELECT * FROM event_rules WHERE id=?", (rule_id,)).fetchone()
    if row is None or not project_id or row["scope"] != rules.project_scope(project_id):
        raise RuleRefused(f"no rule {rule_id!r} in this project")
    rule = rules.from_row(row)
    if caller not in (rule.created_by, rules.thread_target(rule.to)) and _conductor(db, project_id) != caller:
        raise RuleRefused("Only the rule's writer, its recipient or the project's conductor can turn it off.")
    turn_off(db, rule.id, row["scope"], now)
    return {**_shape(rule), "enabled": False}


def turn_off(db: sqlite3.Connection, rule_id: str, scope: str, now: str) -> None:
    """Disable one rule and prune its scope's stale disabled rules: ``disable``'s
    write, and evaluation's for a rule whose writer left its project."""
    db.execute("UPDATE event_rules SET enabled=0, updated_at=? WHERE id=?", (now, rule_id))
    _prune_disabled(db, scope, now)


def _shape(rule: rules.Rule) -> dict[str, Any]:
    return {
        "id": rule.id, "enabled": rule.enabled, "connector": rule.connector,
        "event_type": rule.event_type, "match": list(rule.match), "action": rule.action,
        "to": rule.to, "mode": rule.mode, "note": rule.note, "written_by": rule.created_by,
    }


def list_rules(db: sqlite3.Connection, *, project_id: str | None, now: str) -> list[dict[str, Any]]:
    """The project's rules with when each last acted and its wakes in the last 24 hours."""
    if not project_id:
        return []
    found = [rules.from_row(row) for row in db.execute(
        "SELECT * FROM event_rules WHERE scope=? ORDER BY created_at, id",
        (rules.project_scope(project_id),),
    ).fetchall()]
    if not found:
        return []
    since = (datetime.fromisoformat(now) - timedelta(hours=24)).isoformat()
    marks = ",".join("?" * len(found))
    stats = {
        row["rule_id"]: row for row in db.execute(
            "SELECT rule_id, MAX(CASE WHEN outcome IN ('woke','digested','dropped') THEN at END) "
            "AS last_fired, SUM(outcome='woke' AND via='rule' AND at>=?) AS wakes "
            f"FROM event_route_decisions WHERE rule_id IN ({marks}) GROUP BY rule_id",
            (since, *(rule.id for rule in found)),
        ).fetchall()
    }
    return [
        {**_shape(rule),
         "last_fired": stats[rule.id]["last_fired"] if rule.id in stats else None,
         "wakes_24h": int(stats[rule.id]["wakes"] or 0) if rule.id in stats else 0}
        for rule in found
    ]


def dry_run(
    db: sqlite3.Connection, *, caller: str, project_id: str | None, connector: Any,
    event_type: Any, facts: Any,
) -> list[dict[str, Any]]:
    """Which of the caller's project's enabled rules these facts match, and what each would do.

    Matching only: it reads no thread, owner or tag, so it reveals nothing the
    caller could not already see in ``list``.
    """
    decl = _decl(connector, event_type)
    if not isinstance(facts, Mapping):
        raise RuleRefused("facts is an object of the event's fields")
    if not project_id:
        return []
    return [
        {**_shape(rule), "would": _would(rule)}
        for rule in rules.lookup(db, connector, decl.event_type, caller)
        if matches(rule, facts)
    ]


def _would(rule: rules.Rule) -> str:
    if rule.action == rules.DROP:
        return f"drop deliveries to {rules.thread_target(rule.to)}"
    who = "the registered owner" if rule.to == rules.REGISTERED_OWNER else rules.thread_target(rule.to)
    return f"{rule.mode} {who}"


def explain(
    db: sqlite3.Connection, *, project_id: str | None, event_id: Any = None, artifact: Any = None,
    thread: Any = None,
) -> dict[str, Any]:
    """Why an event did or didn't reach a chat through a rule: the rule, hold or budget.

    A drop also names any armed watch it left waiting, since a dropped event
    doesn't end a wait.
    """
    if not event_id and not artifact:
        raise RuleRefused("give event_id, or artifact (the file's external id)")
    if not project_id:
        return {"decisions": [], "summary": "Rules cover a project's events; this chat is in no project."}
    where = ["r.scope=?"]
    args: list[Any] = [rules.project_scope(project_id)]
    if event_id:
        where.append("d.event_id=?")
        args.append(event_id)
    if artifact:
        where.append("d.artifact_external_id=?")
        args.append(artifact)
    if thread:
        where.append("d.thread_id=?")
        args.append(thread)
    rows = db.execute(
        "SELECT d.* FROM event_route_decisions d JOIN event_rules r ON r.id=d.rule_id "
        f"WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT ?",
        (*args, _EXPLAIN_LIMIT),
    ).fetchall()
    decisions = [_decision(db, row) for row in rows]
    summary = (
        "No rule acted on this event; routing alone decided where it went."
        if not decisions else f"{len(decisions)} rule outcome(s), newest first."
    )
    return {"decisions": decisions, "summary": summary}


def _decision(db: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    outcome, reason, thread = row["outcome"], row["reason"], row["thread_id"]
    text = {
        rules.WOKE: f"rule {row['rule_id']} woke {thread}",
        rules.DIGESTED: f"rule {row['rule_id']} put it in {thread}'s digest",
        rules.DROPPED: f"rule {row['rule_id']} dropped it for {thread}",
        rules.HELD: f"rule {row['rule_id']} held it from {thread}",
        rules.DEAD_LETTERED: f"rule {row['rule_id']} could not deliver it",
        rules.ENDED: f"rule {row['rule_id']} did not fire: the file had already ended",
        rules.DUPLICATE: f"rule {row['rule_id']}: {thread} already had it",
    }[outcome]
    if row["via"] == "routed" and outcome in (rules.WOKE, rules.DIGESTED):
        text += " (with the delivery routing already made)"
    if reason:
        text += f": {reason}"
    shaped: dict[str, Any] = {
        "at": row["at"], "event_id": row["event_id"], "event_type": row["event_type"],
        "artifact": row["artifact_external_id"], "rule_id": row["rule_id"], "thread_id": thread,
        "outcome": outcome, "reason": reason, "explanation": text,
    }
    if outcome == rules.DROPPED and thread and row["artifact_external_id"]:
        watches = [w["id"] for w in db.execute(
            "SELECT id FROM expected_event_watches WHERE artifact_integration=? AND artifact_type=? "
            "AND artifact_external_id=? AND expected_event=? AND owner_thread_id=? AND status='armed'",
            (row["artifact_integration"], row["artifact_type"], row["artifact_external_id"],
             row["watch_event"], thread),
        ).fetchall()]
        if watches:
            shaped["armed_watches_left_waiting"] = watches
            shaped["explanation"] += (
                f"; armed watch {', '.join(watches)} still waits, since a dropped event doesn't end it")
    return shaped
