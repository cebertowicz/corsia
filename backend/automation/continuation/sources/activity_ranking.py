"""Wake scope coordinators when their Activity order needs attention."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from loguru import logger
from events.delivery import enqueue_event_wake
from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
from infra.database import get_db, write_transaction


def _scopes(db):
    """Yield the designated rankers and their owners."""
    workspace = get_scope_coordinator(WORKSPACE_SCOPE, db=db)
    if workspace:
        row = db.execute('SELECT user_id FROM threads WHERE id=?', (workspace,)).fetchone()
        if row:
            yield row['user_id'], None, workspace
    for row in db.execute("SELECT t.user_id, p.id, p.conductor_thread_id FROM projects p "
                          "JOIN threads t ON t.id=p.conductor_thread_id "
                          "WHERE COALESCE(p.status,'active')!='archived'"):
        yield row['user_id'], row['id'], row['conductor_thread_id']


def _instant(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _last_nudge(db, user_id, scope):
    row = db.execute('SELECT last_enqueued_at FROM activity_rank_nudges '
                     'WHERE user_id=? AND scope=?', (user_id, scope)).fetchone()
    if not row:
        return None
    last = _instant(row[0])
    if last is None:
        raise ValueError('invalid activity nudge timestamp')
    return last


def _eligible(rows, last):
    return any(row['rank'] is None and (last is None or
               (raised := _instant(row.get('raised_at'))) is not None and raised > last)
               for row in rows)


def _enqueue_if_due(db, user_id: str, project_id: str | None, thread_id: str, now: str) -> bool:
    from api.attention import activity_queue

    scope = project_id or WORKSPACE_SCOPE
    current = _instant(now)
    if current is None:
        raise ValueError('invalid activity sweep timestamp')
    last = _last_nudge(db, user_id, scope)
    if last and current - last < timedelta(minutes=15):
        return False
    rows, ranker, _ = activity_queue(db, user_id, project_id=project_id, view='activity')
    unranked = sum(row['rank'] is None for row in rows)
    if not ranker or ranker['thread_id'] != thread_id or not _eligible(rows, last):
        return False
    with write_transaction(db, scope='activity_rank_nudge') as tx:
        # Another sweep may have nudged this scope while the queue was read.
        locked_last = _last_nudge(tx, user_id, scope)
        if locked_last != last or (locked_last and current - locked_last < timedelta(minutes=15)):
            return False
        summary = (f'{unranked} items waiting on the person are not ranked. '
                   'Load attention_rank (tools_load names=["attention_rank"]), '
                   'call it with operation read, then set the order.')
        enqueue_event_wake(thread_id, 'activity.unranked', summary,
                           message_id=uuid4().hex, created_at=now, db=tx)
        tx.execute('INSERT INTO activity_rank_nudges(user_id,scope,last_enqueued_at) VALUES(?,?,?) '
                   'ON CONFLICT(user_id,scope) DO UPDATE SET last_enqueued_at=excluded.last_enqueued_at',
                   (user_id, scope, now))
        return True


class ActivityRankingSource:
    kind = 'activity_ranking'

    def prepare(self, now: str, *, db) -> list[str]:
        connection = db or get_db()
        woken = []
        for user_id, project_id, thread_id in _scopes(connection):
            try:
                if _enqueue_if_due(connection, user_id, project_id, thread_id, now):
                    woken.append(thread_id)
            except Exception:  # A failed scope retries on the next sweep; other scopes still drive.
                logger.exception('activity ranking wake failed for scope {}', project_id or WORKSPACE_SCOPE)
        return woken

    def due(self, _now: str, *, db) -> list:
        return []

    def settle(self, *_args, **_kwargs) -> None:
        return None
