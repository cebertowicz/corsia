"""Wake scope coordinators when their Activity order needs attention."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from loguru import logger
from events.delivery import enqueue_event_wake
from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
from infra.database import write_transaction
from infra.product_events import emit_product_event
from infra.thread_input_rows import event_wake_queued
from plans.public import thread_paused_since

# A ranker holds at most one unread ranking wake: no new wake is sent while the last one waits
# in its queue, so items that arrive meanwhile are ranked by that one turn. Each wake records the
# keys then in the queue; once it is read, the next sweep that finds an unranked key missing from
# that record wakes it again. A key counts from when it entered the queue, not when its item was
# created, so a person task created early that falls due later still wakes the ranker. Until
# ranked, an item shows in Activity below the ranked ones, marked 'Not ranked yet'.
WAKE_EVENT = 'activity.unranked'

# Scope -> the last failure logged for it, so a scope that keeps failing logs once until its
# failure changes or it recovers, not once a sweep.
_failing: dict[str, str] = {}


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
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _last_nudge(db, user_id, scope) -> tuple[str | None, frozenset[str]]:
    """The last wake's time and the queue keys it saw; (None, empty) before the first."""
    row = db.execute('SELECT last_enqueued_at, seen_keys FROM activity_rank_nudges '
                     'WHERE user_id=? AND scope=?', (user_id, scope)).fetchone()
    if not row:
        return None, frozenset()
    if _instant(row[0]) is None:
        raise ValueError('invalid activity nudge timestamp')
    seen = json.loads(row[1])
    if not isinstance(seen, list):
        raise ValueError('invalid activity nudge keys')
    return row[0], frozenset(seen)


def _new_unranked(rows, seen) -> int:
    return sum(row['rank'] is None and row['key'] not in seen for row in rows)


def _enqueue_if_due(db, user_id: str, project_id: str | None, thread_id: str, now: str) -> bool:
    from api.attention import activity_queue

    scope = project_id or WORKSPACE_SCOPE
    current = _instant(now)
    if current is None:
        raise ValueError('invalid activity sweep timestamp')
    if event_wake_queued(db, thread_id, WAKE_EVENT):
        return False
    last, seen = _last_nudge(db, user_id, scope)
    rows, ranker, _ = activity_queue(db, user_id, project_id=project_id, view='activity')
    unranked = sum(row['rank'] is None for row in rows)
    new = _new_unranked(rows, seen)
    if not ranker or ranker['thread_id'] != thread_id or not new:
        return False
    keys = json.dumps(sorted(row['key'] for row in rows))
    with write_transaction(db, scope='activity_rank_nudge') as tx:
        # Another sweep may have nudged this scope while the queue was read.
        if _last_nudge(tx, user_id, scope)[0] != last or event_wake_queued(tx, thread_id, WAKE_EVENT):
            return False
        summary = (f'{unranked} items waiting on the person are not ranked. '
                   'Load attention_rank (tools_load names=["attention_rank"]), '
                   'call it with operation read, then set the order.')
        enqueue_event_wake(thread_id, WAKE_EVENT, summary,
                           message_id=uuid4().hex, created_at=now, db=tx)
        tx.execute('INSERT INTO activity_rank_nudges(user_id,scope,last_enqueued_at,seen_keys) VALUES(?,?,?,?) '
                   'ON CONFLICT(user_id,scope) DO UPDATE SET last_enqueued_at=excluded.last_enqueued_at, '
                   'seen_keys=excluded.seen_keys', (user_id, scope, now, keys))
    emit_product_event('activity_rank', 'wake', thread_id=thread_id, user_id=user_id,
                       properties={'scope_kind': 'project' if project_id else 'workspace',
                                   'unranked': unranked, 'new': new})
    return True


class ActivityRankingSource:
    kind = 'activity_ranking'

    def prepare(self, now: str, *, db) -> list[str]:
        woken = []
        for user_id, project_id, thread_id in _scopes(db):
            scope = project_id or WORKSPACE_SCOPE
            # Nothing wakes a paused lane (the same read rule evaluation uses, R7). No nudge is
            # stamped, so after the unpause the first sweep that finds an item raised since the
            # last nudge wakes the ranker once; nothing is replayed.
            if thread_paused_since(db, thread_id) is not None:
                continue
            try:
                if _enqueue_if_due(db, user_id, project_id, thread_id, now):
                    woken.append(thread_id)
            except Exception as exc:  # A failed scope retries on the next sweep; other scopes still drive.
                failure = f'{type(exc).__name__}: {exc}'
                if _failing.get(scope) != failure:
                    logger.exception('activity ranking wake failed for scope {}; logged once until it changes', scope)
                    _failing[scope] = failure
                continue
            if _failing.pop(scope, None) is not None:
                logger.info('activity ranking wake recovered for scope {}', scope)
        return woken

    def due(self, _now: str, *, db) -> list:
        return []

    def settle(self, *_args, **_kwargs) -> None:
        return None
