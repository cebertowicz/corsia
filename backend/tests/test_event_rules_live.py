"""Rule execution is live and delivered watches end."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from connections import binder
from events.platform import deliver_platform_event_sync
from infra.database import get_db, write_transaction
from tests.event_routing_helpers import mk_session, register_pr, pr_payload, route


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


def test_every_merges_wake_over_digest(isolated_db):
    owner = mk_session('owner')
    register_pr('owner/repo', 7, owner)
    with write_transaction(None, scope='test_rules') as db:
        _rule(db, 'digest', mode='digest')
        _rule(db, 'wake', mode='wake', to='thread:' + owner)
    delivered = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome) for d in delivered] == [(owner, 'woke')]


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


def test_drop_runs_before_binding(isolated_db):
    owner = mk_session('owner')
    binder.create_rule(connector='github', fact='repo', value='owner/repo', destination_thread_id=owner)
    with write_transaction(None, scope='test_rules') as db:
        _rule(db, 'stop', action='drop')
    assert deliver_platform_event_sync('github', _event(), 'pr.merged')[0].outcome == 'dropped'
    db = get_db()
    assert db.execute("SELECT 1 FROM artifact_identities WHERE external_id='owner/repo#7'").fetchone() is None
    db.close()


@pytest.mark.parametrize('enabled', [True, False])
def test_drop_pass_maps_any_drop_row_to_suppressed_with_its_id(isolated_db, enabled):
    from events import router

    with write_transaction(None, scope='custom_cancel_drop') as db:
        db.execute("UPDATE event_rules SET enabled=0 WHERE action='drop'")
        _rule(db, 'custom-cancel-drop', action='drop',
              event_type='webhook.github.check_suite_completed',
              conditions=[{'field': 'conclusion', 'op': 'eq', 'value': 'cancelled'}])
        db.execute('UPDATE event_rules SET enabled=? WHERE id=?',
                   (int(enabled), 'custom-cancel-drop'))
    payload = {'repository': {'full_name': 'owner/repo'},
               'check_suite': {'conclusion': 'cancelled', 'head_sha': 'abc'}}
    with (patch.object(router, 'dropping_rule', wraps=router.dropping_rule) as dropping,
          patch.object(router, 'check_suite_pr_metadata_from_event') as resolve_pr):
        result = router._rule_drop_result('webhook.github.check_suite_completed', payload)
    dropping.assert_called_once()
    resolve_pr.assert_not_called()
    assert result == (router.RouteResult(router.RouteOutcome.SUPPRESSED, reason='custom-cancel-drop')
                      if enabled else None)


def test_selection_returns_registry_routing_state(isolated_db):
    from events import router
    from events.identity import resolve_identity

    owner = mk_session('owner')
    register_pr('owner/repo', 7, owner)
    identity = resolve_identity('webhook.github.pr_merged', _event())
    selected = router._select_route('webhook.github.pr_merged', _event(), identity,
                                    identity.external_id, None, None, False)
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


def test_explicit_recipient_handles_ownerless_event_with_default_mode(isolated_db):
    recipient = mk_session('recipient')
    with write_transaction(None, scope='explicit_rule') as db:
        _rule(db, 'explicit', to='thread:' + recipient, mode='default')
    delivered = deliver_platform_event_sync('github', _event(), 'pr.merged')
    assert [(d.thread_id, d.outcome) for d in delivered] == [(recipient, 'digested')]


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
        (None, 'dead_lettered', 'owner_inactive')]


def test_project_drop_row_applies_only_in_the_artifact_owners_scope(isolated_db):
    with write_transaction(None, scope='project_rule') as db:
        _rule(db, 'project-drop', scope='project:one', action='drop')
    assert deliver_platform_event_sync('github', _event(), 'pr.merged')[0].outcome == 'dead_lettered'


@pytest.mark.asyncio
async def test_project_drop_row_applies_on_webhook_path_only_in_owners_project(isolated_db):
    from events.router import RouteOutcome, RouteResult

    register_pr('owner/repo', 7, mk_session('in-one', project_id='one'))
    register_pr('owner/repo', 8, mk_session('in-two', project_id='two'))
    with write_transaction(None, scope='project_rule') as db:
        _rule(db, 'project-drop', scope='project:one', action='drop', event_type='*')
    with patch('events.router.delivery.deliver_to_thread') as deliver:
        dropped = await route('webhook.github.pr_opened', pr_payload('owner/repo', 7))
        kept = await route('webhook.github.pr_opened', pr_payload('owner/repo', 8))
    assert dropped == RouteResult(RouteOutcome.SUPPRESSED, reason='project-drop')
    assert kept.outcome != RouteOutcome.SUPPRESSED
    assert [c.args[0] for c in deliver.call_args_list] == ['in-two']


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
