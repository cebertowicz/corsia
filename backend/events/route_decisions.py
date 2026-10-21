"""Bounded, scoped explanations for real rule deliveries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from events.envelope import EventEnvelope


def record(db, envelope: EventEnvelope, *, rule_ids: list[str],
           thread_id: str | None, outcome: str, scope: str) -> None:
    now = datetime.now(timezone.utc)
    ref = envelope.artifact_ref or {}
    db.executemany(
        'INSERT INTO event_route_decisions '
        '(event_id,rule_id,thread_id,outcome,scope,artifact_integration,artifact_type,artifact_external_id,at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        [(envelope.event_id, rule_id, thread_id, outcome, scope,
          ref.get('integration'), ref.get('type'), ref.get('external_id'), now.isoformat())
         for rule_id in rule_ids],
    )
    db.execute(
        'DELETE FROM event_route_decisions WHERE id IN '
        '(SELECT id FROM event_route_decisions WHERE at < ? ORDER BY at,id LIMIT 1000)',
        ((now - timedelta(days=30)).isoformat(),),
    )


def explain(db, *, scopes: tuple[str, ...], event_id: str | None = None,
            artifact_ref: dict | None = None, limit: int = 100) -> list[dict]:
    sql = 'SELECT event_id,rule_id,thread_id,outcome,at FROM event_route_decisions WHERE scope IN ('
    sql += ','.join('?' for _ in scopes) + ') '
    sql += 'AND at >= ? '
    args: list[Any] = [*scopes, (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()]
    if event_id:
        sql += 'AND event_id=? '
        args.append(event_id)
    else:
        ref = artifact_ref or {}
        sql += 'AND artifact_integration=? AND artifact_type=? AND artifact_external_id=? '
        args.extend([ref.get('integration'), ref.get('type'), ref.get('external_id')])
    return [dict(row) for row in db.execute(sql + 'ORDER BY id DESC LIMIT ?', (*args, limit))]
