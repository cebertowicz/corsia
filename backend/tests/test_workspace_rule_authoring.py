"""The event-rules tool works for agents outside projects."""
import asyncio
import json

from infra.database import get_db
from infra.delegation_scope import WORKSPACE_SCOPE_ID
from tests.event_routing_helpers import mk_session
from tests.test_workspace_rule_routing import _account, _agent, _deliver, _rows
from tools.event_rules import EventRulesTool


def _tool(thread, **kwargs):
    result = asyncio.run(EventRulesTool().execute(_thread_id=thread, **kwargs))
    return result if result.startswith(('Error:', 'Refused:')) else json.loads(result)


def test_workspace_rule_create_list_test_explain_and_disable(isolated_db):
    _agent('writer')
    rule = _tool('writer', operation='create', connector='gmail', event_type='email.received',
                 match=[{'field': 'from_domain', 'op': 'eq', 'value': 'example.com'}], mode='wake')
    assert _rows('SELECT scope FROM event_rules WHERE id=?', rule['id'])[0]['scope'] == WORKSPACE_SCOPE_ID
    assert [r['id'] for r in _tool('writer', operation='list')['rules']] == [rule['id']]
    matches = _tool('writer', operation='test', connector='gmail', event_type='email.received',
                    facts={'from_domain': 'example.com'})['matches']
    assert [r['id'] for r in matches] == [rule['id']]
    account = _account('all')
    _deliver(account, 'new-message')
    [decision] = _rows('SELECT event_id FROM event_route_decisions WHERE rule_id=?', rule['id'])
    explained = _tool('writer', operation='explain', event_id=decision['event_id'])
    assert [r['rule_id'] for r in explained['decisions']] == [rule['id']]
    assert not _tool('writer', operation='disable', rule_id=rule['id'])['enabled']


def test_workspace_refuses_repository_other_recipient_and_wrong_scope(isolated_db):
    _agent('writer')
    _agent('other')
    repository = _tool('writer', operation='create', connector='github', event_type='pr.merged',
                       match=[{'field': 'repo', 'op': 'eq', 'value': 'owner/repo'}])
    assert repository == ('Refused: Naming a repository needs a project that tracks it; '
                          'this chat is in no project.')
    recipient = _tool('writer', operation='create', connector='gmail', event_type='email.received',
                      to='thread:other')
    assert recipient.startswith('Refused: Your rules deliver only to you')
    assert 'conductor' not in recipient.lower()
    scope = _tool('writer', operation='create', connector='gmail', event_type='email.received',
                  scope='project:other')
    assert WORKSPACE_SCOPE_ID in scope


def test_temporary_agent_cannot_write_workspace_rules(isolated_db):
    _agent('writer')
    mk_session('worker', spawned_by='writer')
    result = _tool('worker', operation='create', connector='gmail', event_type='email.received')
    assert "temporary agent can't write rules" in result


def test_workspace_intake_refusals_do_not_include_other_projects(isolated_db):
    from events.rule_declarations import intake_refusals
    from tests.test_workspace_rule_routing import _project

    _project()
    _agent('writer')
    _agent('project-writer', 'p')
    db = get_db()
    try:
        columns = {row[1] for row in db.execute('PRAGMA table_info(event_intake_records)')}
        assert {'event_id', 'event_type', 'refusal_code', 'producer', 'origin', 'at', 'outcome'} <= columns
        from infra.time import utc_now_iso

        for producer in ('writer', 'project-writer'):
            db.execute("INSERT INTO event_intake_records (event_id,event_type,refusal_code,producer,origin,at,outcome,rules_matched) "
                       "VALUES (?, 'sample.test', 'invalid', ?, 'agent', ?, 'refused', 0)",
                       (producer, producer, utc_now_iso()))
        assert [r['producer'] for r in intake_refusals(db, project_id=None, event_type='sample.test')] == ['writer']
    finally:
        db.close()
