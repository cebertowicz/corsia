import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('title,expected', [('Alpha Conductor', 'Beta Conductor'), ('Custom', 'Custom')])
async def test_project_rename_respects_conductor_title(isolated_db, monkeypatch, title, expected):
    from api import projects
    from infra.database import get_db

    db = get_db()
    db.execute('INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)',
               ('owner', 'u', title, 'old', 'old'))
    db.execute('INSERT INTO projects (id,name,path,created_at,updated_at,conductor_thread_id) VALUES (?,?,?,?,?,?)',
               ('p', 'Alpha', '/alpha', 'old', 'old', 'owner'))
    db.commit()
    db.close()
    monkeypatch.setattr(projects, 'authenticate', lambda _: {'id': 'u'})
    await projects.update_project('test', 'p', projects.ProjectUpdate(name='Beta'))
    db = get_db()
    row = db.execute('SELECT title,updated_at FROM threads WHERE id=?', ('owner',)).fetchone()
    db.close()
    assert row['title'] == expected
    if title == 'Custom':
        assert row['updated_at'] == 'old'
