"""Rule execution is live, inspection is read-only, and delivered watches end."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from connections import binder
from events.envelope import normalize
from events.platform import deliver_platform_event_sync
from infra.database import get_db, write_transaction
from tests.event_routing_helpers import mk_session, register_pr, pr_payload, route
from tools.event_rules import inspect_rules


def _rule(db, id, *, action='deliver', to='registered_owner', mode='digest',
          scope='global', event_type='pr.merged', conditions=None):
    db.execute('''INSERT INTO event_rules
        (id,created_at,updated_at,scope,connector,event_type,"match","on",action,"to",mode,precedence)
        VALUES (?,'a','b',?,'github',?,?,'every',?,?,?,0)''',
        (id, scope, event_type, json.dumps(conditions or []), action,
         None if action == 'drop' else to, None if action == 'drop' else mode))


def _event():
    payload = pr_payload('owner/repo', 7, action='closed')
    payload['pull_request']['merged'] = True
    return payload


def test_every_merges_wake_over_digest_and_traces_all_rules(isolated_db):
    owner = mk_session('owner')
    register_pr('owner/repo', 7, owner)
    with write_transaction(None, scope='test_rules') as db:
        _rule(db, 'digest', mode='digest')
        _rule(db, 'wake', mode='wake', to='thread:' + owner)
    delivered = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome) for d in delivered] == [(owner, 'woke')]
    db = get_db()
    metadata = json.loads(db.execute("SELECT metadata FROM messages WHERE id LIKE 'platform-event:%'").fetchone()[0])
    assert set(metadata['provenance']['rule_trace']) == {'builtin:0', 'digest', 'wake'}
    assert {r['rule_id'] for r in db.execute('SELECT rule_id FROM event_route_decisions')} == {
        'builtin:0', 'digest', 'wake'}
    db.close()


@pytest.mark.parametrize('mode', ['wake', 'digest', 'drop'])
def test_watch_resolves_only_after_rule_delivers(isolated_db, mode):
    from tests.test_plan_event_wait_boundary import _owner, _watching, _armed

    db = get_db()
    actor = _owner(db)
    register_pr('owner/repo', 7, 'owner')
    _, node = _watching(db, actor, external_id='owner/repo#7', event='pr.merged')
    with write_transaction(db, scope='test_rules'):
        _rule(db, 'watch-rule', action='drop' if mode == 'drop' else 'deliver',
              mode=mode if mode != 'drop' else None)
    db.close()
    with patch('plans.public.resolve_delivered_event', wraps=__import__(
        'plans.public', fromlist=['resolve_delivered_event']).resolve_delivered_event) as resolve:
        delivered = deliver_platform_event_sync('github', _event(), 'pr.merged')
    db = get_db()
    assert _armed(db, node.id) == (1 if mode == 'drop' else 0)
    assert resolve.call_count == (0 if mode == 'drop' else 1)
    assert delivered[0].outcome == {'wake': 'woke', 'digest': 'digested', 'drop': 'dropped'}[mode]
    if mode != 'drop':
        with patch('plans.public.resolve_delivered_event') as duplicate_resolve:
            assert deliver_platform_event_sync('github', _event(), 'pr.merged')[0].outcome == 'duplicate'
        duplicate_resolve.assert_not_called()
    db.close()


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


def test_drop_runs_before_binding_and_test_matches_live_without_writes(isolated_db):
    owner = mk_session('owner')
    binder.create_rule(connector='github', fact='repo', value='owner/repo', destination_thread_id=owner)
    with write_transaction(None, scope='test_rules') as db:
        _rule(db, 'stop', action='drop')
    envelope = normalize('github', _event(), 'pr.merged')
    db = get_db()
    db.execute('PRAGMA query_only=ON')
    before = db.total_changes
    preview = inspect_rules(db, action='test', project_id=None, event={
        'connector': 'github', 'type': envelope.type, 'facts': envelope.facts,
        'artifact_ref': envelope.artifact_ref, 'event_class': envelope.event_class,
    })
    assert db.total_changes == before
    assert preview == {'dropped_by': 'stop', 'targets': [], 'delivered': False}
    db.execute('PRAGMA query_only=OFF')
    db.close()
    assert deliver_platform_event_sync('github', _event(), 'pr.merged')[0].outcome == 'dropped'
    db = get_db()
    assert db.execute("SELECT 1 FROM artifact_identities WHERE external_id='owner/repo#7'").fetchone() is None
    db.close()


def test_readonly_preview_predicts_first_seen_binding_and_delivery(isolated_db):
    owner = mk_session('owner')
    binder.create_rule(connector='github', fact='repo', value='owner/repo', destination_thread_id=owner)
    envelope = normalize('github', _event(), 'pr.merged')
    db = get_db()
    db.execute('PRAGMA query_only=ON')
    before = db.total_changes
    preview = inspect_rules(db, action='test', project_id=None, event={
        'connector': 'github', 'type': envelope.type, 'facts': envelope.facts,
        'artifact_ref': envelope.artifact_ref, 'event_class': envelope.event_class,
    })
    assert preview['targets'][0]['thread_id'] == owner
    assert preview['targets'][0]['disposition'] == 'owner_digest'
    assert db.total_changes == before
    db.execute('PRAGMA query_only=OFF')
    db.close()
    live = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome) for d in live] == [(owner, 'digested')]


def test_explicit_recipient_handles_ownerless_event_with_default_mode(isolated_db):
    recipient = mk_session('recipient')
    with write_transaction(None, scope='explicit_rule') as db:
        _rule(db, 'explicit', to='thread:' + recipient, mode='default')
    delivered = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome) for d in delivered] == [(recipient, 'digested')]


@pytest.mark.asyncio
async def test_webhook_delivers_to_two_recipients_and_replay_is_deduplicated(isolated_db, monkeypatch):
    from unittest.mock import AsyncMock
    from events.router import route_event, RouteOutcome
    from shared_send_fixtures import _insert_session, _messages, _install_fake_native_context_agent
    from subagents import codex_runtime

    _install_fake_native_context_agent(monkeypatch)
    for thread in ('owner', 'extra'):
        _insert_session(thread, user_id='user-1', model='codex/gpt-5-codex', spawned_by=None)
    register_pr('owner/repo', 7, 'owner')
    with write_transaction(None, scope='two_recipients') as db:
        _rule(db, 'extra-wake', to='thread:extra', mode='wake', event_type='webhook.github.pr_merged')

    async def feed(*_args, **kwargs):
        turn_id = kwargs['successor_turn_id']
        await kwargs['bind_input_turn'](turn_id, None)
        kwargs['mark_accepted']()
        return kwargs['preadmitted_turn']

    with (patch.object(codex_runtime, 'ensure_connection_available', lambda db=None: None),
          patch.object(codex_runtime, 'has_thread_id', AsyncMock(return_value=False)),
          patch.object(codex_runtime, 'has_prior_messages', AsyncMock(return_value=False)),
          patch.object(codex_runtime, 'feed_main', AsyncMock(side_effect=feed))):
        for _ in range(2):
            result = await route_event('webhook.github.pr_merged', _event(), delivery_id='delivery-7')
            assert result.outcome == RouteOutcome.ACCEPTED
    for thread in ('owner', 'extra'):
        rows = [r for r in _messages(thread) if r['role'] == 'system'
                and json.loads(r['metadata']).get('routed_event')]
        assert len(rows) == 1
        assert json.loads(rows[0]['metadata'])['provenance']['rule_trace']


def test_list_is_project_scoped_and_limited(isolated_db):
    with write_transaction(None, scope='test_rules') as db:
        _rule(db, 'own', scope='project:one')
        _rule(db, 'other', scope='project:two')
    db = get_db()
    db.execute('PRAGMA query_only=ON')
    rows = inspect_rules(db, action='list', project_id='one')['rules']
    assert 'own' in {r['id'] for r in rows}
    assert 'other' not in {r['id'] for r in rows}
    assert len(inspect_rules(db, action='list', project_id='one', limit=1)['rules']) == 1
    db.close()


def test_designation_unicode_lookup_enabled_filter_and_current_designee(isolated_db):
    from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator

    owner = mk_session('owner')
    replacement = mk_session('replacement')
    assert set_scope_coordinator(WORKSPACE_SCOPE, owner)
    with write_transaction(None, scope='test_migrated_designation') as db:
        # Deliberately differs from json.dumps spacing/escaping, as a legacy
        # SQLite JSON serializer did. Lookup must use values, never JSON text.
        db.execute('''INSERT INTO event_rules
            (id,created_at,updated_at,scope,connector,"match","on",action,"to",mode)
            VALUES ('unicode','a','b','designation:workspace','gmail',?,
                    'first_seen','deliver',?,'default')''',
            ('[{"field":"from","op":"eq","value":"Élise@例.example"}]', 'thread:' + owner))
    rule = binder.ensure_designation_rule(connector='gmail', fact='from', value='Élise@例.example')
    assert rule.id == 'unicode'
    assert set_scope_coordinator(WORKSPACE_SCOPE, replacement)
    assert binder.evaluate({'from': 'Élise@例.example'}, 'gmail')[0].destination_thread_id == replacement
    binder.disable_rule(rule.id)
    new = binder.ensure_designation_rule(connector='gmail', fact='from', value='Élise@例.example')
    assert new.id != rule.id
    assert new.enabled and new.destination_thread_id == replacement


@pytest.mark.parametrize('mode', ['wake', 'digest'])
def test_explicit_missing_recipient_is_not_delivered(isolated_db, mode):
    with write_transaction(None, scope='missing_rule') as db:
        _rule(db, 'missing', to='thread:missing', mode=mode)
    delivered = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome, d.reason) for d in delivered] == [
        ('missing', 'dead_lettered', 'owner_inactive')]


def test_preview_uses_artifact_scope_instead_of_callers_project(isolated_db):
    with write_transaction(None, scope='project_rule') as db:
        _rule(db, 'project-drop', scope='project:one', action='drop')
    envelope = normalize('github', _event(), 'pr.merged')
    db = get_db()
    db.execute('PRAGMA query_only=ON')
    preview = inspect_rules(db, action='test', project_id='one', event={
        'connector': 'github', 'type': envelope.type, 'facts': envelope.facts,
        'artifact_ref': envelope.artifact_ref, 'event_class': envelope.event_class})
    assert preview['dropped_by'] is None
    assert preview['targets'][0]['disposition'] == 'dead_letter'
    db.execute('PRAGMA query_only=OFF')
    db.close()
    assert deliver_platform_event_sync('github', _event(), 'pr.merged')[0].outcome == 'dead_lettered'


def test_explain_rejects_ambiguous_pr_reference(isolated_db):
    db = get_db()
    with pytest.raises(ValueError, match='type pull-request and external_id owner/repo#7'):
        inspect_rules(db, action='explain', project_id=None, artifact_ref={
            'integration': 'github', 'type': 'pull_request',
            'external_id': 'https://github.com/owner/repo/pull/7'})
    db.close()


def test_retention_deletes_at_most_one_thousand_old_decisions(isolated_db):
    from events import route_decisions

    envelope = normalize('github', _event(), 'pr.merged')
    with write_transaction(None, scope='retention') as db:
        db.executemany('''INSERT INTO event_route_decisions
            (event_id,rule_id,outcome,scope,at) VALUES (?,'old','digested','global','2000-01-01')''',
            [(str(i),) for i in range(1001)])
        route_decisions.record(db, envelope, rule_ids=['new'], thread_id=None,
                               outcome='dropped', scope='global')
        assert db.execute("SELECT count(*) FROM event_route_decisions WHERE rule_id='old'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM event_route_decisions WHERE rule_id='new'").fetchone()[0] == 1


def test_designation_reads_are_batched_across_forty_scopes(isolated_db):
    db = get_db()
    with write_transaction(db, scope='designation_scale'):
        db.executemany('''INSERT INTO event_rules
            (id,created_at,updated_at,scope,connector,"match","on",action,"to",mode)
            VALUES (?,'a','b',?,'gmail','[{"field":"from","op":"eq","value":"sender"}]',
                    'first_seen','deliver','thread:placeholder','default')''',
            [(f'designation-{i}', f'designation:project-{i}') for i in range(40)])
    statements = []
    db.set_trace_callback(statements.append)
    assert binder.evaluate({'from': 'sender'}, 'gmail', db=db) == []
    db.set_trace_callback(None)
    assert len(statements) == 2  # One rule read and one batch project designation read.
    db.close()


def test_query_budget_uses_rule_index_at_ten_times_configured_rows(isolated_db):
    from events.rules import matching_rules

    db = get_db()
    with write_transaction(db, scope='synthetic_10x'):
        for i in range(36):  # Four seeded rows plus 36 configured rows = 10x fresh state.
            _rule(db, f'agent-{i}', to=f'thread:agent-{i}')
    statements = []
    db.set_trace_callback(statements.append)
    rows = matching_rules({}, 'github', on='every', event_type='pr.merged', scopes=('global',), db=db)
    db.set_trace_callback(None)
    assert len(rows) == 36
    assert len(statements) == 1
    plan = db.execute('''EXPLAIN QUERY PLAN SELECT * FROM event_rules
        WHERE connector='github' AND "on"='every' AND enabled=1
          AND event_type IN ('pr.merged','*') AND scope IN ('global')
        ORDER BY precedence,created_at,id''').fetchall()
    assert any('idx_event_rules_match' in r['detail'] for r in plan)
    db.close()
