import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('title,expected', [('Alpha Conductor', 'Beta Conductor'), ('Custom', 'Custom')])
async def test_project_rename_respects_conductor_title(isolated_db, monkeypatch, title, expected):
    from api import projects
    from infra.database import get_db
    from infra import state

    db = get_db()
    db.execute('INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)',
               ('owner', 'u', title, 'old', 'old'))
    db.execute('INSERT INTO projects (id,name,path,created_at,updated_at,conductor_thread_id) VALUES (?,?,?,?,?,?)',
               ('p', 'Alpha', '/alpha', 'old', 'old', 'owner'))
    db.commit()
    db.close()
    monkeypatch.setattr(projects, 'authenticate', lambda _: {'id': 'u'})
    notifications = []
    events = []
    monkeypatch.setattr(projects, 'notify_projects_changed', notifications.append)
    monkeypatch.setattr(state, 'broadcast_event', lambda *args: events.append(args))
    await projects.update_project('test', 'p', projects.ProjectUpdate(name='Beta'))
    assert notifications == ['u']
    if title != 'Custom':
        assert events[0][2]['type'] == 'session_updated'
        assert events[0][2]['title'] == expected
    else:
        assert events == []
    db = get_db()
    row = db.execute('SELECT title,updated_at FROM threads WHERE id=?', ('owner',)).fetchone()
    db.close()
    assert row['title'] == expected
    if title == 'Custom':
        assert row['updated_at'] == 'old'
