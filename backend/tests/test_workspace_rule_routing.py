"""Workspace event rules follow the same routing and account reach as project rules."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from connections.connected_accounts import create_account
from connections.google.provider import GOOGLE
from connections.mail.email_screen import Screen
from connections.microsoft.mail import Message
from events import observe_rules, rules
from events.connectors import mail_poller
from events.rule_outcomes import RuleEvent
from events.resolve import Target
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from infra.delegation_scope import WORKSPACE_SCOPE_ID
from tests.event_routing_helpers import mk_session
from tools.event_rules import EventRulesTool


def _write(sql, *args):
    db = get_db()
    try:
        db.execute(sql, args)
        db.commit()
    finally:
        db.close()


def _rows(sql, *args):
    db = get_db()
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def _project():
    now = datetime.now(timezone.utc).isoformat()
    _write('INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)', 'p', 'P', now, now)


def _agent(name, project=None):
    mk_session(name, project_id=project)
    _write('UPDATE threads SET agent_enabled=1 WHERE id=?', name)


def _rule(name, writer, project=None):
    response = asyncio.run(EventRulesTool().execute(
        _thread_id=writer, operation='create', connector='gmail', event_type='email.received',
        match=[{'field': 'from_domain', 'op': 'eq', 'value': 'example.com'}], mode='wake'))
    assert not response.startswith(('Refused:', 'Error:')), response
    rule = json.loads(response)
    _write('UPDATE event_rules SET id=? WHERE id=?', name, rule['id'])


def _account(mode, outside=False):
    account = create_account(GOOGLE.name, 'person@example.test',
                             reach={'mode': 'all' if mode == 'corsia' else mode,
                                    'project_ids': [], 'outside_projects': outside},
                             created_by=('system', 'test'), provider_subject='subject')
    if mode == 'corsia':
        _write('UPDATE connected_accounts SET reach_mode=? WHERE id=?', mode, account['id'])
    return str(account['id'])


def _deliver(account, message_id):
    message = Message(message_id, f'thread-{message_id}',
                      {'from': 'sender@example.com', 'subject': 'hello', 'date': '2026-11-10T00:00:00Z'}, 'body')
    payload = mail_poller.event_payload(account, message,
        Screen('screened', 'mail', 'later', False, 'none', 'local'), 'position', 'google')
    assert mail_poller._deliver(account, payload)


def _wakes():
    return _rows("SELECT thread_id FROM messages WHERE id LIKE 'platform-event:%'")


@pytest.mark.parametrize(('mode', 'outside', 'wakes'), [
    ('all', False, True), ('selected', False, False), ('selected', True, True), ('corsia', False, False),
])
def test_unowned_mail_respects_account_reach(isolated_db, mode, outside, wakes):
    _agent('writer')
    if mode == 'corsia':
        set_corsia_agent('writer')
    _rule('rule', 'writer')
    account = _account(mode, outside)
    _deliver(account, 'message')
    assert bool(_wakes()) is wakes
    decisions = _rows("SELECT outcome FROM event_route_decisions WHERE rule_id='rule'")
    assert [row['outcome'] for row in decisions] == (['woke'] if wakes else [])
    assert _rows("SELECT enabled FROM event_rules WHERE id='rule'")[0]['enabled'] == 1


def test_anchored_lookup_separates_scopes_and_missing_thread(isolated_db):
    _project()
    _agent('outside')
    _agent('inside', 'p')
    _rule('outside-rule', 'outside')
    _rule('inside-rule', 'inside', 'p')
    db = get_db()
    try:
        assert [r.id for r in rules.lookup(db, 'gmail', 'email.received', 'outside')] == ['outside-rule']
        assert [r.id for r in rules.lookup(db, 'gmail', 'email.received', 'inside')] == ['inside-rule']
        assert rules.lookup(db, 'gmail', 'email.received', 'missing') == []
        traced = []
        db.set_trace_callback(traced.append)
        rules.lookup(db, 'gmail', 'email.received', 'outside')
        db.set_trace_callback(None)
        reads = [sql for sql in traced if sql.lstrip().upper().startswith('SELECT')]
        assert len(reads) == 1
        plan = db.execute('EXPLAIN QUERY PLAN ' + reads[0]).fetchall()
        assert any('idx_event_rules_type' in row[3] for row in plan)
    finally:
        db.close()


@pytest.mark.parametrize(('initial', 'after'), [(None, 'p'), ('p', None)])
def test_writer_moving_out_of_scope_disables_old_rule(isolated_db, initial, after):
    _project()
    _agent('writer', initial)
    _agent('anchor', initial)
    _rule('rule', 'writer', initial)
    _write('UPDATE threads SET project_id=? WHERE id=?', after, 'writer')
    db = get_db()
    try:
        event = RuleEvent('event', 'gmail', 'email.received', '', {'from_domain': 'example.com'}, None)
        target = Target(thread='anchor', resolution='direct', relationship='owner', disposition='owner_wake')
        plan = rules.evaluate(db, event, [target])
        assert plan is not None
        assert [(outcome.reason, outcome.outcome) for outcome in plan.settled] == [
            ("its writer left the rule's scope; rule turned off", 'dead_lettered')]
        assert db.execute("SELECT enabled FROM event_rules WHERE id='rule'").fetchone()[0] == 0
    finally:
        db.close()


def test_observe_rule_applies_outside_projects_only_to_its_writer(isolated_db):
    _agent('writer')
    _agent('other')
    _write("INSERT INTO event_rules (id,created_at,updated_at,kind,scope,connector,origin,created_by,"
           "authority_ref,tool,item_type) VALUES ('observe','now','now','observe',?,'gmail','agent:writer',"
           "'writer','self','mail','message')", WORKSPACE_SCOPE_ID)
    db = get_db()
    try:
        assert [r.id for r in observe_rules.applicable(db, None, 'writer', 'mail')] == ['observe']
        assert observe_rules.applicable(db, None, 'other', 'mail') == []
    finally:
        db.close()
