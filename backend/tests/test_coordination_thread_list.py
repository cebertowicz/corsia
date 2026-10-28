import json

import pytest


@pytest.mark.asyncio
async def test_list_threads_includes_owned_visible_coordination_separately(isolated_db, monkeypatch):
    from api import threads as route
    from infra.database import get_db

    db = get_db()
    for tid, owner, spawned in [('normal', 'u', None), ('workspace', 'u', None),
                                ('project', 'u', None), ('foreign', 'other', None),
                                ('hidden', 'u', 'normal'), ('archived', 'u', None)]:
        db.execute('INSERT INTO threads (id,user_id,title,created_at,updated_at,spawned_by) VALUES (?,?,?,?,?,?)',
                   (tid, owner, tid, 't', 't', spawned))
    for pid, tid in [('a', 'project'), ('b', 'foreign'), ('c', 'hidden'), ('d', 'archived')]:
        db.execute('INSERT INTO projects (id,name,path,created_at,updated_at,conductor_thread_id) VALUES (?,?,?,?,?,?)',
                   (pid, pid, '/' + pid, 't', 't', tid))
    db.execute("UPDATE projects SET status='archived' WHERE id='d'")
    db.execute("UPDATE threads SET project_id='d' WHERE id='archived'")
    db.execute('INSERT OR REPLACE INTO settings (key,value,updated_at) VALUES (?,?,?)',
               ('corsia_agent_thread_id', json.dumps({'session_id': 'workspace'}), 't'))
    db.commit()
    db.close()
    monkeypatch.setattr(route, 'authenticate', lambda _: {'id': 'u'})
    result = await route.list_threads('test')
    assert [r['id'] for r in result['threads']] == ['normal']
    assert {r['id']: r['coordination'] for r in result['coordination']} == {
        'workspace': 'corsia', 'project': 'conductor',
    }
    assert all('last_message' in r and 'archived' in r for r in result['coordination'])
