"""The coordinating agent's read and replacement of an Activity order."""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.activity_rank_scope import ranker_for
from infra.activity_ranking import replace_ranks, validate_order
from tools.base import Tool
from tools.delegation import _require_context


def _run(thread_id: str, actor: dict, operation: str, items: list | None) -> str:
    from api.attention import activity_queue

    db = get_db()
    try:
        project_id = actor['project_id']
        # A read takes no write lock; set validates and replaces in one write transaction.
        lock = nullcontext(db) if operation == 'read' else write_transaction(db, scope='attention_rank')
        with lock as connection:
            scope, ranker = ranker_for(connection, actor['user_id'], project_id)
            if not ranker or ranker['thread_id'] != thread_id:
                label = ranker['label'] if ranker else 'No ranker is assigned'
                return f"Error: {label} ranks this scope."
            # A project without a conductor reads the workspace order. The workspace
            # ranker ranks the whole workspace, never replaces it with a filtered list.
            if project_id and scope == 'workspace':
                project_id = None
            rows, _, _ = activity_queue(connection, actor['user_id'], project_id=project_id, view='activity')
            if operation == 'read':
                now = datetime.now(timezone.utc)
                readable = []
                for row in rows:
                    try:
                        raised = datetime.fromisoformat(row['raised_at'].replace('Z', '+00:00'))
                        if raised.tzinfo is None:
                            raised = raised.replace(tzinfo=timezone.utc)
                        age_seconds = max(0, int((now - raised).total_seconds()))
                    except (AttributeError, TypeError, ValueError):
                        age_seconds = None
                    readable.append({key: row[key] for key in ('key', 'kind', 'title', 'source', 'rank')}
                                    | {'age_seconds': age_seconds})
                return json.dumps({'scope': scope, 'items': readable})
            if operation != 'set':
                return 'Error: operation must be read or set.'
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                return 'Error: set requires items, a list of {key, reason}.'
            try:
                validate_order(items, {row['key'] for row in rows})
            except ValueError as exc:
                return f'Error: {exc}'
            replace_ranks(connection, actor['user_id'], scope, thread_id, items,
                          datetime.now(timezone.utc).isoformat())
            logger.info('activity rank set scope={} count={} ranker={}', scope, len(items), thread_id)
            remaining = len(rows) - len(items)
            return (f'Ranked {len(items)} for {scope}; the person sees this order now.'
                    + (f' {remaining} remain unranked.' if remaining else ''))
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
                'person in one line, at most 90 characters. State only what the item or '
                'priorities show; if unsure, say what it waits for. Unlisted items stay '
                'below ranked items; set replaces the previous order. Call on a ranking '
                'wake and when your scope priorities change.')

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'additionalProperties': False, 'properties': {
            'operation': {'type': 'string', 'enum': ['read', 'set']},
            'items': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                'properties': {'key': {'type': 'string'}, 'reason': {'type': 'string', 'maxLength': 90}},
                'required': ['key', 'reason']}},
        }, 'required': ['operation']}

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor = context
        return await run_in_db(_run, thread_id, actor, kwargs.get('operation'), kwargs.get('items'))
