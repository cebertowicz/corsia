"""Rule execution is live and delivered watches end."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from connections import binder
from events.platform import deliver_platform_event_sync
from infra.database import get_db, write_transaction
from tests.event_routing_helpers import mk_session, register_pr, pr_payload, route


def _event():
    payload = pr_payload('owner/repo', 7, action='closed')
    payload['pull_request']['merged'] = True
    return payload


@pytest.mark.asyncio
async def test_legacy_webhook_name_watch_resolves_on_platform_path(isolated_db):
    from tests.test_plan_event_wait_boundary import _owner, _watching, _armed

    db = get_db()
    actor = _owner(db)
    register_pr('owner/repo', 7, 'owner')
    _, node = _watching(db, actor, external_id='owner/repo#7', event='webhook.github.pr_merged')
    db.close()
    with patch('events.delivery.send_thread_input'):
        result = await route('webhook.github.pr_merged', _event())
    assert result.thread_id == 'owner'
    db = get_db()
    assert _armed(db, node.id) == 0
    db.close()


def test_selection_returns_registry_routing_state(isolated_db):
    from events import router
    from events.identity import resolve_identity

    owner = mk_session('owner')
    register_pr('owner/repo', 7, owner)
    identity = resolve_identity('webhook.github.pr_merged', _event())
    selected = router._select_route('webhook.github.pr_merged', _event(), identity,
                                    identity.external_id, None, False)
    assert not isinstance(selected, router.RouteResult)
    assert set(selected[2]) == {'identity', 'owner', 'subscribers', 'triage'}


def test_first_seen_binding_rule_delivers_on_platform_path(isolated_db):
    owner = mk_session('owner')
    binder.create_rule(connector='github', fact='repo', value='owner/repo', destination_thread_id=owner)
    live = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome) for d in live] == [(owner, 'digested')]


def test_binding_rule_on_numeric_fact_matches_its_stored_string(isolated_db):
    owner = mk_session('owner')
    binder.create_rule(connector='github', fact='pr_number', value=7, destination_thread_id=owner)
    assert binder.evaluate({'pr_number': 7}, 'github')[0].destination_thread_id == owner
    assert binder.evaluate({'pr_number': 8}, 'github') == []


def test_designation_unicode_lookup_enabled_filter_and_current_designee(isolated_db):
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator

    owner = mk_session('owner')
    replacement = mk_session('replacement')
    assert set_scope_coordinator(WORKSPACE_SCOPE, owner)
    with write_transaction(None, scope='test_migrated_designation') as db:
        # Deliberately differs from json.dumps spacing/escaping, as a legacy
        # SQLite JSON serializer did. Lookup must use values, never JSON text.
        db.execute('''INSERT INTO event_rules
            (id,created_at,updated_at,scope,connector,"match","on",action,"to")
            VALUES ('unicode','a','b','designation:workspace','gmail',?,
                    'first_seen','deliver',?)''',
            ('[{"field":"from","op":"eq","value":"Élise@例.example"}]', 'thread:' + owner))
    rule = binder.ensure_designation_rule(connector='gmail', fact='from', value='Élise@例.example')
    assert rule.id == 'unicode'
    assert set_scope_coordinator(WORKSPACE_SCOPE, replacement)
    assert binder.evaluate({'from': 'Élise@例.example'}, 'gmail')[0].destination_thread_id == replacement
    binder.disable_rule(rule.id)
    new = binder.ensure_designation_rule(connector='gmail', fact='from', value='Élise@例.example')
    assert new.id != rule.id
    assert new.enabled and new.destination_thread_id == replacement


def test_designation_reads_are_batched_across_forty_scopes(isolated_db):
    db = get_db()
    with write_transaction(db, scope='designation_scale'):
        db.executemany('''INSERT INTO event_rules
            (id,created_at,updated_at,scope,connector,"match","on",action,"to")
            VALUES (?,'a','b',?,'gmail','[{"field":"from","op":"eq","value":"sender"}]',
                    'first_seen','deliver','thread:placeholder')''',
            [(f'designation-{i}', f'designation:project-{i}') for i in range(40)])
    statements = []
    db.set_trace_callback(statements.append)
    assert binder.evaluate({'from': 'sender'}, 'gmail', db=db) == []
    db.set_trace_callback(None)
    assert len(statements) == 2  # One rule read and one batch project designation read.
    db.close()


def test_seeded_every_rows_cost_one_indexed_read(isolated_db):
    from events.rules import matching_rules

    db = get_db()
    statements = []
    db.set_trace_callback(statements.append)
    rows = matching_rules({'webhook': True}, 'github', on='every',
                          event_type='pr.merged', scope='global', db=db)
    db.set_trace_callback(None)
    assert rows and len(statements) == 1
    plan = db.execute('''EXPLAIN QUERY PLAN SELECT * FROM event_rules
        WHERE connector='github' AND "on"='every' AND enabled=1
          AND event_type IN ('pr.merged','*') AND scope=?
        ORDER BY precedence,created_at,id''', ('global',)).fetchall()
    assert any('idx_event_rules_match' in r['detail'] for r in plan)
    db.close()
