"""The coordinating agent's read and replacement of an Activity order."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from infra.database import get_db, write_transaction
from infra.product_events import emit_product_event
from infra.db_async import run_in_db
from infra.activity_rank_scope import ranker_for
from infra.activity_ranking import REASON_MAX_CHARS, replace_ranks, validate_order
from tools.base import Tool
from tools.delegation import _require_context


def _age_seconds(raised_at: str | None, now: datetime) -> int | None:
    try:
        raised = datetime.fromisoformat(str(raised_at).replace('Z', '+00:00'))
    except ValueError:
        return None
    if raised.tzinfo is None:
        raised = raised.replace(tzinfo=timezone.utc)
    return max(0, int((now - raised).total_seconds()))


def _refusal(ranker: dict | None) -> str:
    if not ranker:
        return 'Error: no thread ranks this scope; it has no conductor or workspace agent.'
    return f"Error: {ranker['label']} ranks this scope, not you."


def _read(scope: str, rows: list[dict]) -> str:
    """The first QUEUE_ITEMS_CAP items in the person's order, as the Activity list sends them."""
    from api.attention import QUEUE_ITEMS_CAP

    now = datetime.now(timezone.utc)
    readable = [{key: row[key] for key in ('key', 'kind', 'title', 'source', 'rank')}
                | {'age_seconds': _age_seconds(row.get('raised_at'), now)}
                for row in rows[:QUEUE_ITEMS_CAP]]
    return json.dumps({'scope': scope, 'count': len(rows), 'items': readable})


def _set(db, thread_id: str, user_id: str, project_id: str | None, scope: str,
         rows: list[dict], items: Any) -> str:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return 'Error: set requires items, a list of {key, reason}.'
    try:
        current_keys = {row['key'] for row in rows}
        validate_order(items, current_keys)
    except ValueError as exc:
        return f'Error: {exc}'
    # The queue is composed outside the lock; the write re-checks only the ranker. A key that
    # leaves the queue meanwhile is ignored on read and dropped on the next set, which also
    # forgets it was seen, so its return wakes the ranker.
    with write_transaction(db, scope='attention_rank') as tx:
        locked_scope, ranker = ranker_for(tx, user_id, project_id)
        if locked_scope != scope or not ranker or ranker['thread_id'] != thread_id:
            return _refusal(ranker)
        replace_ranks(tx, user_id, scope, thread_id, items, datetime.now(timezone.utc).isoformat(), current_keys)
    remaining = len(rows) - len(items)
    logger.info('activity rank set scope={} count={} ranker={}', scope, len(items), thread_id)
    emit_product_event('activity_rank', 'set', thread_id=thread_id, user_id=user_id,
                       properties={'scope_kind': 'workspace' if scope == 'workspace' else 'project',
                                   'ranked': len(items), 'unranked': remaining})
    return (f'Ranked {len(items)} for {scope}; the person sees this order now.'
            + (f' {remaining} remain unranked.' if remaining else ''))


def _run(thread_id: str, actor: dict, operation: str, items: list | None) -> str:
    from api.attention import activity_queue

    if operation not in ('read', 'set'):
        return 'Error: operation must be read or set.'
    db = get_db()
    try:
        user_id, project_id = actor['user_id'], actor['project_id']
        scope, ranker = ranker_for(db, user_id, project_id)
        if not ranker or ranker['thread_id'] != thread_id:
            return _refusal(ranker)
        # A project without a conductor reads the workspace order. The workspace
        # ranker ranks the whole workspace, never replaces it with a filtered list.
        queue_project = None if scope == 'workspace' else project_id
        rows, _, _ = activity_queue(db, user_id, project_id=queue_project, view='activity')
        if operation == 'read':
            return _read(scope, rows)
        return _set(db, thread_id, user_id, project_id, scope, rows, items)
    finally:
        db.close()


class AttentionRankTool(Tool):
    @property
    def name(self) -> str:
        return 'attention_rank'

    @property
    def description(self) -> str:
        return ('Set the order the person works through Activity. Read the items, then set '
                'items [{key, reason}] in order. Rank by the scope priorities, putting what '
                'blocks agents or a date the person set first. Write each reason for the '
                f'person in one line, at most {REASON_MAX_CHARS} characters. State only what the item or '
                'priorities show; if unsure, say what it waits for. Unlisted items stay '
                'below ranked items; set replaces the previous order. Call on a ranking '
                'wake and when your scope priorities change.')

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'additionalProperties': False, 'properties': {
            'operation': {'type': 'string', 'enum': ['read', 'set']},
            'items': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                'properties': {'key': {'type': 'string'}, 'reason': {'type': 'string', 'maxLength': REASON_MAX_CHARS}},
                'required': ['key', 'reason']}},
        }, 'required': ['operation']}

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor = context
        return await run_in_db(_run, thread_id, actor, kwargs.get('operation'), kwargs.get('items'))
