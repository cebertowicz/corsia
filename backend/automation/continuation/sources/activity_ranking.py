"""Note newly unranked Activity items for scope coordinators."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from loguru import logger
from infra.passive_changes import REOFFER_UNWRITTEN, append_thread_context_note
from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
from infra.database import get_db, write_transaction
from infra.product_events import emit_product_event
from plans.public import thread_paused_since

# A set forgets seen keys no longer in the queue, so a returning item is new again.
KEYS_MAX_CHARS = 500

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
    """The last note's time and the queue keys it saw; (None, empty) before the first."""
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


# Compare queue keys before writing so the note and its deduplication stamp commit together.
def _enqueue_if_due(read_db, db, user_id: str, project_id: str | None, thread_id: str, now: str) -> bool:
    from api.attention import activity_queue

    scope = project_id or WORKSPACE_SCOPE
    current = _instant(now)
    if current is None:
        raise ValueError('invalid activity sweep timestamp')
    last, seen = _last_nudge(read_db, user_id, scope)
    rows, ranker, _ = activity_queue(read_db, user_id, project_id=project_id, view='activity')
    unranked = sum(row['rank'] is None for row in rows)
    new_keys = sorted(row['key'] for row in rows if row['rank'] is None and row['key'] not in seen)
    new = len(new_keys)
    if not ranker or ranker['thread_id'] != thread_id or not new:
        return False
    keys = json.dumps(sorted(row['key'] for row in rows))
    with write_transaction(db, scope='activity_rank_nudge') as tx:
        # Another sweep may have nudged this scope while the queue was read.
        if _last_nudge(tx, user_id, scope) != (last, seen):
            return False
        key_list = ', '.join(new_keys)
        if len(key_list) > KEYS_MAX_CHARS:
            key_list = key_list[:KEYS_MAX_CHARS - 1].rstrip() + '…'
        summary = (f'Activity: {new} new items waiting on the person, auto-ordered by urgency '
                   f'(keys: {key_list}). Review the order with attention_rank (tools_load names=["attention_rank"]) '
                   'when you next work through Activity.')
        append_thread_context_note(tx, thread_id=thread_id, summary=summary,
                                   details={'chip': None, 'kind': 'activity_rank', REOFFER_UNWRITTEN: True})
        tx.execute('INSERT INTO activity_rank_nudges(user_id,scope,last_enqueued_at,seen_keys) VALUES(?,?,?,?) '
                   'ON CONFLICT(user_id,scope) DO UPDATE SET last_enqueued_at=excluded.last_enqueued_at, '
                   'seen_keys=excluded.seen_keys', (user_id, scope, now, keys))
    emit_product_event('activity_rank', 'digest', thread_id=thread_id, user_id=user_id,
                       properties={'scope_kind': 'project' if project_id else 'workspace',
                                   'unranked': unranked, 'new': new})
    return True


class ActivityRankingSource:
    kind = 'activity_ranking'

    def prepare(self, now: str, *, db) -> list[str]:
        """Record passive notes and return no threads for the runner to drive.

        The runner's own sweep passes ``db=None``: reads take the pooled connection, and each
        scope's write owns its transaction.
        """
        read_db = db if db is not None else get_db()
        for user_id, project_id, thread_id in list(_scopes(read_db)):
            scope = project_id or WORKSPACE_SCOPE
            # Leave paused lanes unstamped so the first sweep after unpause can note new items.
            if thread_paused_since(read_db, thread_id) is not None:
                continue
            try:
                _enqueue_if_due(read_db, db, user_id, project_id, thread_id, now)
            except Exception as exc:  # A failed scope retries on the next sweep; other scopes still drive.
                failure = f'{type(exc).__name__}: {exc}'
                if _failing.get(scope) != failure:
                    logger.exception('activity ranking digest failed for scope {}; logged once until it changes', scope)
                    _failing[scope] = failure
                continue
            if _failing.pop(scope, None) is not None:
                logger.info('activity ranking digest recovered for scope {}', scope)
        return []

    def due(self, _now: str, *, db) -> list:
        return []

    def settle(self, *_args, **_kwargs) -> None:
        return None
