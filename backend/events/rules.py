"""One matcher for first-seen bindings and the seeded every-event rules.

Only eq/present, deliver/drop and their live consumers are supported. ``every``
rows are the global seeds: the drop rows and the tag and owner delivery rows.
Ownership and disposition remain resolve_targets' responsibility.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from events.envelope import EventEnvelope
from events.resolve import Target
from infra.database import get_db

THREAD_PREFIX = 'thread:'
TAG_DESTINATION = '$corsia_tag'
TAG_RELATIONSHIP = 'tag'  # A Target's relationship when the tag row matched.


@dataclass(frozen=True)
class EventRule:
    id: str
    scope: str
    connector: str
    event_type: str
    match: list[dict[str, Any]]
    on: str
    action: str
    to: str | None
    precedence: int
    enabled: bool


def thread_destination(to: str | None) -> str | None:
    """The destination of a ``thread:`` target, or None for any other target."""
    if to is not None and to.startswith(THREAD_PREFIX):
        return to.removeprefix(THREAD_PREFIX)
    return None


def from_row(row) -> EventRule:
    return EventRule(
        row['id'], row['scope'], row['connector'], row['event_type'],
        json.loads(row['match']), row['on'], row['action'], row['to'],
        row['precedence'], bool(row['enabled']),
    )


def list_rules(connector: str, *, on: str, event_type: str = '*',
               scope: str | None = None, enabled_only: bool = True,
               db=None) -> list[EventRule]:
    conn = db or get_db()
    try:
        sql = 'SELECT * FROM event_rules WHERE connector=? AND "on"=? AND event_type IN (?,\'*\') '
        args: list[Any] = [connector, on, event_type]
        if enabled_only:
            sql += 'AND enabled=1 '
        if scope is not None:
            sql += 'AND scope=? '
            args.append(scope)
        rows = conn.execute(sql + 'ORDER BY precedence,created_at,id', args).fetchall()
        return [from_row(row) for row in rows]
    finally:
        if db is None:
            conn.close()


def matches(rule: EventRule, facts: dict[str, Any]) -> bool:
    for condition in rule.match:
        actual = facts.get(condition['field'])
        if condition['op'] == 'present':
            if actual is None:
                return False
        else:  # The schema admits only eq and present.
            expected = condition['value']
            equal = str(actual) == expected if isinstance(expected, str) else actual == expected
            if not equal:
                return False
    return True


def matching_rules(facts: dict[str, Any], connector: str, *, on: str,
                   event_type: str = '*', scope: str | None = None,
                   db=None) -> list[EventRule]:
    return [rule for rule in list_rules(
        connector, on=on, event_type=event_type, scope=scope, db=db,
    ) if matches(rule, facts)]


def _every_rules(envelope: EventEnvelope, db) -> list[EventRule]:
    return matching_rules(
        envelope.facts, envelope.connector or '', on='every',
        event_type=envelope.type, scope='global', db=db,
    )


def dropping_rule(envelope: EventEnvelope, *, db=None) -> str | None:
    """The first matching drop row's id. Drop rows match event facts only."""
    return next((r.id for r in _every_rules(envelope, db) if r.action == 'drop'), None)


def evaluate(envelope: EventEnvelope, *,
             resolve_owner: Callable[[], list[Target]], db=None) -> list[Target]:
    """The first matching deliver row's target, by precedence.

    Callers run ``dropping_rule`` first. The owner callback is lazy, so an
    event whose first row does not deliver to its owner never binds an artifact.
    """
    return decide(_every_rules(envelope, db), envelope,
                  resolve_owner=resolve_owner, db=db)


def decide(rows: list[EventRule], envelope: EventEnvelope, *,
           resolve_owner: Callable[[], list[Target]], db=None) -> list[Target]:
    """The first deliver row decides. The schema admits only the two shapes below
    for an ``every`` deliver row, so any other outcome is a broken rule table."""
    first = next((r for r in rows if r.action == 'deliver'), None)
    if first is None or first.to not in ('registered_owner', THREAD_PREFIX + TAG_DESTINATION):
        return [Target(None, 'rule', 'none', reason='rule_table_invalid',
                       disposition='dead_letter')]
    if first.to == 'registered_owner':
        return resolve_owner()
    return [_tag_target(str(envelope.facts['corsia_tag']), db)]


def _tag_target(thread_id: str, db) -> Target:
    from events.delivery import thread_exists_and_active

    if not thread_exists_and_active(thread_id, db=db):
        return Target(thread_id, 'rule', TAG_RELATIONSHIP, reason='invalid_tag_target',
                      disposition='dead_letter')
    return Target(thread_id, 'rule', TAG_RELATIONSHIP, disposition='owner_wake')
