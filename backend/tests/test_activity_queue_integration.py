"""The composed queue preserves the dock view and supports ranking."""
import json

from plans import service
from tests.test_attention_queue import db, raise_decision  # noqa: F401
from tests.test_attention_queue_api import _attention_client


def test_activity_view_and_scope_order(db, monkeypatch):
    raise_decision(db, 'child', 'child')
    plan = service.ensure_plan('thread', 'root', db=db)
    task = service.add_node(plan.id, kind='task', title='Person task',
                            acceptance_criteria='Done', assignee='user', db=db)
    db.execute("INSERT INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id',?,'t')",
               (json.dumps({'session_id':'root'}),))
    db.commit()
    client = _attention_client(db, monkeypatch)
    default = client.get('/api/attention/queue').json()
    activity = client.get('/api/attention/queue?view=activity').json()
    assert default['count'] == 1
    assert {r['renderer'] for r in default['items']} == {'decision'}
    assert activity['count'] == 2 == len(activity['items'])
    assert {r['kind'] for r in activity['items']} == {'decision', 'task'}
    assert activity['ranker'] == {'thread_id':'root', 'label':'Parent work'}
    db.execute("INSERT INTO activity_ranks VALUES (?,?,?,?,?,?,?)",
               ('user-1','workspace',f'task:{task.id}',1,'Ready first','root','2026-01-01'))
    db.commit()
    ordered = client.get('/api/attention/queue?view=activity&project_id=project-1').json()
    assert ordered['current']['key'] == f'task:{task.id}'
    assert ordered['current']['rank']['reason'] == 'Ready first'
    assert client.get('/api/attention/queue?view=activity&project_id=missing').status_code == 404
