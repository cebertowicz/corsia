"""Migration recovery uses the same task and attention rules as live comments."""
import asyncio

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import person_comment_attention
from infra.database import get_db, write_transaction
from plans import person_comment_recovery as recovery, person_comment_task


def _marker(db):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?,CURRENT_TIMESTAMP)", (recovery.MARKER, '1'))
    db.commit()


def _seed(db, monkeypatch, name):
    from plans import public, user_ask
    with monkeypatch.context() as patch:
        patch.setattr(user_ask, 'comment_tasks_changed', lambda *_: None)
        patch.setattr(public, 'reconcile_person_comment_task', lambda *_: None)
        artifact = seed_document(db, 'project:example', name=name)
        root, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        add_comment(db, artifact, author_id='author', parent_id=root['id'])
    return artifact


def _tasks(db, artifact):
    return db.execute('SELECT node_id FROM plan_node_artifact_links WHERE artifact_id=? AND provenance=?',
                      (artifact, person_comment_task.PROVENANCE)).fetchall()


def test_startup_recovers_open_and_delivered_only_once_and_preserves_read(isolated_db, monkeypatch, caplog):
    caplog.set_level('INFO', logger=recovery.log.name)
    db = get_db()
    seed_thread(db, 'conductor')
    seed_thread(db, 'author')
    db.commit()
    monkeypatch.setattr(person_comment_task, '_supervisor', lambda *_: 'conductor')
    opened = _seed(db, monkeypatch, 'open.md')
    delivered = _seed(db, monkeypatch, 'delivered.md')
    read = _seed(db, monkeypatch, 'read.md')
    db.execute("UPDATE doc_feedback_batches SET state='delivered' WHERE artifact_id=? AND recipient_id='ui'", (delivered,))
    db.commit()
    with write_transaction(db, scope='dismiss_recovery_test') as conn:
        person_comment_attention.dismiss(conn, read, 'owner-token')
    assert not _tasks(db, opened)
    _marker(db)
    from app_lifecycle import _run_startup_background
    async def drive():
        pass
    asyncio.run(_run_startup_background(drive()))
    assert len(_tasks(db, opened)) == len(_tasks(db, delivered)) == 1
    assert not _tasks(db, read)
    assert not recovery._pending()
    assert 'candidates=3 created=2 failed=0' in caplog.text
    _marker(db)  # A retry is idempotent even if a crash preceded marker clearance.
    asyncio.run(recovery.run())
    assert len(_tasks(db, opened)) == len(_tasks(db, delivered)) == 1
    assert not recovery._pending()


def test_failure_keeps_marker_and_retry_finishes(isolated_db, monkeypatch):
    db = get_db()
    seed_thread(db, 'conductor')
    seed_thread(db, 'author')
    db.commit()
    monkeypatch.setattr(person_comment_task, '_supervisor', lambda *_: 'conductor')
    artifact = _seed(db, monkeypatch, 'failure.md')
    _marker(db)
    with monkeypatch.context() as patch:
        def fail(_):
            raise RuntimeError('injected failure')
        patch.setattr(recovery, '_reconcile', fail)
        asyncio.run(recovery.run())
    assert recovery._pending()
    assert not _tasks(db, artifact)
    asyncio.run(recovery.run())
    assert len(_tasks(db, artifact)) == 1
    assert not recovery._pending()


def test_covering_ask_is_preserved(isolated_db, monkeypatch):
    from test_person_comment_attention import _ask_node, _raise
    from registry import document_supervisor

    monkeypatch.setattr(document_supervisor, 'effective_supervisor', lambda *_args, **_kwargs: ({'id': 'conductor'}, None))
    db = get_db()
    seed_thread(db, 'conductor')
    seed_thread(db, 'author')
    db.commit()
    monkeypatch.setattr(person_comment_task, '_supervisor', lambda *_: 'conductor')
    artifact = _seed(db, monkeypatch, 'covered.md')
    with write_transaction(db, scope='recovery_ask_test') as conn:
        ask = _ask_node(conn, 'conductor')
        hold = _raise(conn, ask.id, artifact, 'conductor')
    _marker(db)
    asyncio.run(recovery.run())
    assert not _tasks(db, artifact)
    assert db.execute('SELECT cleared_at FROM plan_node_holds WHERE id=?', (hold,)).fetchone()[0] is None
    assert not recovery._pending()


def test_concurrent_passes_create_one_task(isolated_db, monkeypatch):
    db = get_db()
    seed_thread(db, 'conductor')
    seed_thread(db, 'author')
    db.commit()
    monkeypatch.setattr(person_comment_task, '_supervisor', lambda *_: 'conductor')
    artifact = _seed(db, monkeypatch, 'concurrent.md')
    _marker(db)

    async def both():
        await asyncio.gather(recovery.run(), recovery.run())

    asyncio.run(both())
    assert len(_tasks(db, artifact)) == 1
    assert not recovery._pending()


def test_shutdown_joins_inflight_database_work_and_keeps_marker(isolated_db, monkeypatch):
    import threading
    from app_lifecycle import _run_startup_background

    db = get_db()
    _marker(db)
    entered, release = threading.Event(), threading.Event()
    original = recovery._page

    def blocked_page(after):
        entered.set()
        assert release.wait(5)
        return original(after)

    monkeypatch.setattr(recovery, '_page', blocked_page)

    async def exercise():
        async def drive():
            await asyncio.Event().wait()
        task = asyncio.create_task(_run_startup_background(drive()))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('shutdown cancellation was lost')

    asyncio.run(exercise())
    assert recovery._pending()
