"""One matcher for first-seen bindings and every-event delivery rules.

Only eq/present, deliver/drop and their live consumers are supported. Ownership
and disposition remain resolve_targets' responsibility, not a second registry.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from events.envelope import EventEnvelope
from events.disposition import delivery_disposition
from events.resolve import Target
from infra.database import get_db


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
    mode: str | None
    precedence: int
    enabled: bool


def from_row(row) -> EventRule:
    return EventRule(
        row['id'], row['scope'], row['connector'], row['event_type'],
        json.loads(row['match']), row['on'], row['action'], row['to'],
        row['mode'], row['precedence'], bool(row['enabled']),
    )


def list_rules(connector: str, *, on: str, event_type: str = '*',
               scopes: tuple[str, ...] | None = None, enabled_only: bool = True,
               db=None) -> list[EventRule]:
    conn = db or get_db()
    try:
        sql = 'SELECT * FROM event_rules WHERE connector=? AND "on"=? AND event_type IN (?,\'*\') '
        args: list[Any] = [connector, on, event_type]
        if enabled_only:
            sql += 'AND enabled=1 '
        if scopes is not None:
            sql += 'AND scope IN (' + ','.join('?' for _ in scopes) + ') '
            args.extend(scopes)
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
                   event_type: str = '*', scopes: tuple[str, ...] | None = None,
                   db=None) -> list[EventRule]:
    return [rule for rule in list_rules(
        connector, on=on, event_type=event_type, scopes=scopes, db=db,
    ) if matches(rule, facts)]


@dataclass
class Evaluation:
    targets: list[Target] = field(default_factory=list)
    rule_ids: dict[str | None, list[str]] = field(default_factory=dict)
    dropped_by: str | None = None


def evaluate(envelope: EventEnvelope, *, scope: str = 'global',
             resolve_owner: Callable[[], list[Target]], db=None) -> Evaluation:
    """Drop first, resolve the owner once, then merge delivery rules per thread.

    The owner callback is lazy: dropped events cannot bind an artifact or resolve
    a watch. A read-only preview supplies a read-only ownership callback.
    """
    rules = matching_rules(
        envelope.facts, envelope.connector or '', on='every',
        event_type=envelope.type, scopes=tuple(dict.fromkeys(('global', scope))), db=db,
    )
    drop = next((r for r in rules if r.action == 'drop'), None)
    if drop is not None:
        return Evaluation(dropped_by=drop.id)
    # Webhook defaults are explicit seeded rows. Other producers use rule 0.
    owner_rules = [r for r in rules if r.to == 'registered_owner']
    use_default = not envelope.facts.get('webhook')
    owner_targets = resolve_owner() if owner_rules or use_default else []
    result = Evaluation()
    explicit_availability: dict[str, bool] = {}

    def merge(target: Target, rule_id: str, mode: str | None = 'default') -> None:
        if mode in ('wake', 'digest') and target.thread is not None:
            target = replace(target, disposition='owner_' + mode)
        key = target.thread
        ids = result.rule_ids.setdefault(key, [])
        if rule_id not in ids:
            ids.append(rule_id)
        existing = next((t for t in result.targets if t.thread == key), None)
        if existing is None:
            result.targets.append(target)
        elif target.disposition == 'owner_wake' and existing.disposition != 'owner_wake':
            result.targets[result.targets.index(existing)] = target

    if use_default:
        for target in owner_targets:
            merge(target, 'builtin:0')
    for rule in rules:
        if rule.to == 'registered_owner':
            for target in owner_targets:
                merge(target, rule.id, rule.mode)
        else:
            destination = (rule.to or '')[7:]
            if destination == '$corsia_tag':
                destination = str(envelope.facts['corsia_tag'])
            from events.delivery import thread_exists_and_active

            if destination not in explicit_availability:
                explicit_availability[destination] = thread_exists_and_active(destination, db=db)
            available = explicit_availability[destination]
            merge(Target(destination, 'rule', 'owner',
                         reason=None if available else 'owner_inactive',
                         disposition=(delivery_disposition('owner', envelope.event_class)
                                      if available else 'dead_letter')),
                  rule.id, rule.mode if available else 'default')
    # A live explicit destination handles an ownerless event. Rule 0's missing
    # owner is not an extra recipient to dead-letter alongside that delivery.
    if any(target.thread is not None for target in result.targets):
        result.targets = [target for target in result.targets if target.thread is not None]
        result.rule_ids.pop(None, None)
    return result


def event_scope(envelope: EventEnvelope, *, db) -> str:
    """Resolve scope from registered ownership, never from external event facts."""
    from registry import models

    ref = envelope.artifact_ref
    if ref is None:
        return 'global'
    state = models.routing_registrations(ref['integration'], ref['type'], ref['external_id'], db=db)
    owner = state.get('owner') or state.get('triage')
    if owner:
        row = db.execute('SELECT project_id FROM threads WHERE id=?', (owner['parent_thread_id'],)).fetchone()
        if row and row['project_id']:
            return 'project:' + row['project_id']
    return 'global'
