"""Event rule inspection and declared-event emission using the live evaluator."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from events.envelope import EventEnvelope, _event_class
from events.resolve import resolve_targets
from events.rules import evaluate, event_scope
from events.route_decisions import explain, explain_refusals
from infra.database import get_db
from infra.db_async import run_in_db
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema

logger = logging.getLogger(__name__)


def inspect_rules(db, *, action: str, project_id: str | None,
                  connector: str | None = None, event: dict | None = None,
                  event_id: str | None = None, event_type: str | None = None,
                  artifact_ref: dict | None = None,
                  limit: int = 100) -> dict:
    if artifact_ref is not None and (not isinstance(artifact_ref, dict) or
            not all(isinstance(artifact_ref.get(k), str) and artifact_ref[k]
                    for k in ('integration', 'type', 'external_id'))):
        raise ValueError('artifact_ref needs integration, type and external_id; for a GitHub PR use '
                         '{integration: github, type: pull-request, external_id: owner/repo#7}')
    if artifact_ref and artifact_ref.get('integration') == 'github' and (
            artifact_ref['type'] == 'pull_request' or '://' in artifact_ref['external_id']):
        raise ValueError('For a GitHub PR use type pull-request and external_id owner/repo#7, not a URL')
    scope = 'project:' + project_id if project_id else 'global'
    scopes = tuple(dict.fromkeys(('global', scope)))
    if action == 'list':
        sql = 'SELECT * FROM event_rules WHERE scope IN (' + ','.join('?' for _ in scopes) + ') '
        args = list(scopes)
        if connector:
            sql += 'AND connector=? '
            args.append(connector)
        rows = db.execute(sql + 'ORDER BY precedence,created_at,id LIMIT ?', (*args, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['match'] = json.loads(item['match'])
            result.append(item)
        return {'rules': result, 'limit': limit}
    if action == 'explain':
        if event_type:
            return {'refusals': explain_refusals(db, scopes=scopes, event_type=event_type, limit=limit)}
        if not event_id and not artifact_ref:
            raise ValueError('explain needs event_id, event_type, or artifact_ref (integration, type, external_id)')
        return {'decisions': explain(db, scopes=scopes, event_id=event_id,
                                     artifact_ref=artifact_ref, limit=limit)}
    if action != 'test':
        raise ValueError('action must be list, test, explain, or emit; rules are read-only here and emit sends an event')
    if not event or not isinstance(event.get('connector'), str) or not isinstance(event.get('type'), str):
        raise ValueError('test needs event.connector, event.type and event.facts (normalized facts)')
    facts = event.get('facts')
    if not isinstance(facts, dict):
        raise ValueError('event.facts must be a JSON object of normalized facts')
    ref = event.get('artifact_ref')
    if ref is not None and (not isinstance(ref, dict) or
                           not all(isinstance(ref.get(k), str) for k in ('integration', 'type', 'external_id'))):
        raise ValueError('event.artifact_ref needs integration, type and external_id')
    from events.intake import resolve_event_type

    resolved = resolve_event_type(db, f"{event['connector']}.{event['type']}")
    if resolved is None:
        raise ValueError('event type is not declared')
    decl, _ = resolved
    event_class = event.get('event_class') or _event_class(decl, facts)
    if event_class not in ('actionable', 'informational'):
        raise ValueError('event.event_class must be actionable or informational; include it for webhook events')
    envelope = EventEnvelope(
        hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest(),
        event['type'], event['connector'], facts, (), ref,
        {'rule_trace': []}, event_class, decl.ownerless_ok if decl else False,
    )
    actual_scope = event_scope(envelope, db=db)
    if actual_scope not in scopes:
        raise ValueError('artifact is outside your project; test an artifact in your project scope')
    evaluated = evaluate(envelope, scope=actual_scope, db=db,
                         resolve_owner=lambda: resolve_targets(envelope, db=db, preview=True))
    return {
        'dropped_by': evaluated.dropped_by,
        'targets': [{'thread_id': t.thread, 'disposition': t.disposition,
                     'rule_ids': evaluated.rule_ids[t.thread], 'reason': t.reason}
                    for t in evaluated.targets],
        'delivered': False,
    }


class EventRulesTool(Tool):
    @property
    def name(self) -> str:
        return 'event_rules'

    @property
    def description(self) -> str:
        return (
            'Inspect rules or emit one declared event. list returns up to 100 rules in your project plus global '
            'rules. test previews the live evaluator on normalized event facts; nothing is bound, delivered '
            'or written. emit sends declared facts as your thread and returns its receipt; event_id is a '
            'retry identity scoped to event_type. explain reads delivery '
            'decisions by event_id or artifact_ref {integration,type,external_id}, or recent emitted-event '
            'refusals by event_type. For a GitHub PR use {integration: github, type: pull-request, '
            'external_id: owner/repo#7}. An empty explain result means no retained decision or refusal '
            'matches; check the event source next. No create/disable.'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            'action': {'type': 'string', 'enum': ['list', 'test', 'explain', 'emit']},
            'connector': {'type': 'string', 'description': 'Optional source filter for list.'},
            'event': {'type': 'object', 'description': (
                'For test: connector, type, facts (normalized facts), optional artifact_ref '
                '{integration,type,external_id} and event_class (actionable or informational; required for webhook types). '
                'Webhook facts include webhook=true, webhook_self_authored and corsia_tag when present.'
            )},
            'event_id': {'type': 'string', 'description': 'For explain or a retry-safe emit identity.'},
            'event_type': {'type': 'string', 'description': 'For emit or explain recent refusals of one declared type.'},
            'facts': {'type': 'object', 'description': 'For emit: declared event facts.'},
            'artifact_ref': {
                'type': 'object',
                'properties': {key: {'type': 'string'} for key in ('integration', 'type', 'external_id')},
                'required': ['integration', 'type', 'external_id'],
                'description': 'GitHub PR: integration=github, type=pull-request, external_id=owner/repo#7 (not URL).',
            },
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        }, ['action'])

    async def execute(self, action: str, connector: str | None = None,
                      event: dict | None = None, event_id: str | None = None,
                      event_type: str | None = None, facts: dict | None = None,
                      artifact_ref: dict | None = None, limit: int = 100, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor = context
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            return 'Error: limit must be an integer from 1 to 100.'
        if action == 'emit':
            if not isinstance(event_type, str) or not event_type:
                return 'Error: emit needs event_type.'
            from events.intake import admit_event, emitted_event_id, record_refusal

            emitted_id = emitted_event_id(event_type, event_id)
            try:
                result = await admit_event(
                    event_type=event_type, facts=facts, origin='agent', producer=thread_id,
                    event_id=emitted_id,
                )
            except Exception:  # noqa: BLE001
                return 'Error: intake_unreachable: event intake is unavailable; retry with event_id.'
            if result.refusal is not None:
                try:
                    await asyncio.to_thread(
                        record_refusal, event_id=emitted_id, event_type=event_type,
                        origin='agent', producer=thread_id, refusal=result.refusal,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception('could not record agent event refusal')
                from events.intake import format_event_error
                return f'Error: {format_event_error(result.refusal.code, result.refusal.text)}'
            return json.dumps({
                'event_id': emitted_id,
                'duplicate': result.duplicate,
                'rules_matched': result.rules_matched,
            }, ensure_ascii=False)

        def inspect() -> dict:
            db = get_db()
            query_only = db.execute('PRAGMA query_only').fetchone()[0]
            try:
                db.execute('PRAGMA query_only=ON')
                return inspect_rules(db, action=action, project_id=actor.get('project_id'),
                                     connector=connector, event=event, event_id=event_id,
                                     event_type=event_type, artifact_ref=artifact_ref, limit=limit)
            finally:
                db.execute(f'PRAGMA query_only={int(query_only)}')
                db.close()
        try:
            return json.dumps(await run_in_db(inspect), ensure_ascii=False)
        except ValueError as exc:
            return f'Error: {exc}'
