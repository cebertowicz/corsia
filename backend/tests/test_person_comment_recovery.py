"""Recovery follows unanswered person threads, not person-attention watermarks."""
import asyncio
import itertools

import pytest

from doc_feedback_helpers import feedback_nodes, seed_document, seed_thread
from infra.database import get_db
from plans import person_comment_recovery as recovery


@pytest.fixture
def data(isolated_db, tmp_path):
    db = get_db()
    seed_thread(db, 'conductor', model=None)
    db.execute("INSERT INTO projects(id,name,conductor_thread_id,created_at,updated_at) VALUES ('example','Example','conductor','t','t')")
    sequence = itertools.count()

    def document(name='document.md', *, exists=True, owner='project:example'):
        artifact = seed_document(db, owner, name=name)
        path = tmp_path / name
        if exists:
            path.write_text('Source text')
        db.execute('UPDATE artifact_identities SET path=?,external_id=? WHERE id=?', (str(path), str(path), artifact))
        db.commit()
        return artifact

    def comment(artifact, *, kind='user', parent=None, resolved=False, timestamp=None):
        index = next(sequence)
        comment_id = f'comment-{index:04d}'
        at = timestamp or f'2026-11-19T00:00:{index:02d}+00:00'
        db.execute('''INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,body,resolved,created_at,updated_at)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (comment_id, artifact, parent, 'Person' if kind == 'user' else 'Agent', kind,
                    f'Message {index}', int(resolved), at, at))
        db.commit()
        return comment_id

    yield db, document, comment
    db.close()


def marker(db):
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?,CURRENT_TIMESTAMP)', (recovery.MARKER, '1'))
    db.commit()


def run(db):
    marker(db)
    asyncio.run(recovery.run())


def held_comments(db):
    return {row[0] for row in db.execute('''SELECT i.comment_id FROM doc_feedback_batch_items i
        JOIN doc_feedback_batches b ON b.id=i.batch_id WHERE b.recipient_id='conductor' AND b.state='delivered' ''')}


def batch(db, artifact, comment_id, *, recipient='ui', state='open'):
    identifier = f'{recipient}-{comment_id}'
    db.execute('''INSERT INTO doc_feedback_batches(id,artifact_id,author_kind,author_id,recipient_id,
        state,opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
        VALUES (?,?,'user','person',?,?,'t','t','t',1,'t','t')''', (identifier, artifact, recipient, state))
    db.execute('INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES (?,?)', (identifier, comment_id))
    db.commit()
    return identifier


def test_mixed_threads_group_latest_person_messages_in_one_conductor_task(data, caplog):
    db, document, comment = data
    artifact = document()
    first = comment(artifact)
    agent_root = comment(artifact, kind='agent')
    reply = comment(artifact, parent=agent_root)
    answered = comment(artifact)
    comment(artifact, kind='agent', parent=answered)
    resolved = comment(artifact, resolved=True)
    comment(artifact, parent=resolved)
    # UI history must not create attention tasks or constrain eligibility.
    batch(db, artifact, first, state='delivered')
    caplog.set_level('INFO', logger=recovery.log.name)
    run(db)
    nodes = feedback_nodes(db, 'conductor')
    assert len(nodes) == 1
    assert nodes[0]['assignee'] != 'user'
    assert held_comments(db) == {first, reply}
    assert 'Message 0' in nodes[0]['description'] and 'Message 2' in nodes[0]['description']
    assert not db.execute("SELECT 1 FROM plan_node_artifact_links WHERE provenance='document.person_comment_task'").fetchone()
    assert not recovery._pending()
    assert 'documents=1' in caplog.text and 'comments=7' in caplog.text
    assert 'skipped_resolved=1' in caplog.text and 'skipped_answered=1' in caplog.text
    run(db)
    assert len(feedback_nodes(db, 'conductor')) == 1


def test_missing_file_and_conductor_are_counted_without_tasks(data, caplog):
    db, document, comment = data
    comment(document('missing.md', exists=False))
    comment(document('no-conductor.md', owner='project:unknown'))
    caplog.set_level('INFO', logger=recovery.log.name)
    run(db)
    assert not feedback_nodes(db, 'conductor')
    assert 'skipped_missing_file=1' in caplog.text
    assert 'skipped_missing_conductor=1' in caplog.text
    assert not recovery._pending()


@pytest.mark.parametrize('state', ['open', 'undelivered'])
def test_pending_conductor_feedback_is_not_duplicated(data, state, caplog):
    db, document, comment = data
    artifact = document()
    root = comment(artifact)
    batch(db, artifact, root, recipient='conductor', state=state)
    caplog.set_level('INFO', logger=recovery.log.name)
    run(db)
    assert not feedback_nodes(db, 'conductor')
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches WHERE recipient_id='conductor'").fetchone()[0] == 1
    assert 'skipped_pending=1' in caplog.text


def test_open_delivered_task_deduplicates_but_finished_task_does_not(data):
    db, document, comment = data
    artifact = document()
    root = comment(artifact)
    run(db)
    node = feedback_nodes(db, 'conductor')[0]
    run(db)
    assert len(feedback_nodes(db, 'conductor')) == 1
    from tests.plan_writer_authorizer import raw_plan_seed
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (node['id'],))
    db.commit()
    run(db)
    assert len(feedback_nodes(db, 'conductor')) == 2
    assert held_comments(db) == {root}


def test_failure_rolls_back_and_retry_finishes_without_duplicate(data, monkeypatch):
    db, document, comment = data
    artifact = document()
    comment(artifact)
    from plans import feedback_nodes as writer
    original = writer.create_feedback_node
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('failure after task creation')
        patch.setattr(writer, 'create_feedback_node', fail)
        run(db)
    assert recovery._pending()
    assert not feedback_nodes(db, 'conductor')
    assert not held_comments(db)
    asyncio.run(recovery.run())
    assert len(feedback_nodes(db, 'conductor')) == 1
    assert not recovery._pending()


def test_concurrent_passes_are_idempotent(data):
    db, document, comment = data
    comment(document())
    marker(db)
    async def both():
        await asyncio.gather(recovery.run(), recovery.run())
    asyncio.run(both())
    assert len(feedback_nodes(db, 'conductor')) == 1
    assert not recovery._pending()


def test_cancellation_keeps_marker(data, monkeypatch):
    db, document, comment = data
    comment(document())
    marker(db)
    def cancel(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(recovery, '_reconcile', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(recovery.run())
    assert recovery._pending()
    assert not feedback_nodes(db, 'conductor')


def test_latest_person_reply_after_agent_response_recovers_and_ties_are_stable(data):
    db, document, comment = data
    artifact = document()
    root = comment(artifact)
    comment(artifact, kind='agent', parent=root)
    reply = comment(artifact, parent=root)
    tied_root = comment(artifact, timestamp='2026-11-19T01:00:00+00:00')
    comment(artifact, kind='agent', parent=tied_root, timestamp='2026-11-19T01:00:00+00:00')
    run(db)
    assert held_comments(db) == {reply}


def test_nonproject_and_retired_documents_are_not_recovered(data):
    db, document, comment = data
    comment(document('personal.md', owner='ui'))
    retired = document('retired.md')
    comment(retired)
    db.execute("UPDATE artifact_identities SET state='retired' WHERE id=?", (retired,))
    db.commit()
    run(db)
    assert not feedback_nodes(db, 'conductor')


def test_cancellation_joins_inflight_document_and_retry_does_not_duplicate(data, monkeypatch):
    import threading

    db, document, comment = data
    comment(document())
    marker(db)
    entered, release = threading.Event(), threading.Event()
    original = recovery._deliver

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(recovery, '_deliver', blocked)

    async def cancel_during_write():
        task = asyncio.create_task(recovery.run())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_write())
    assert recovery._pending()
    assert len(feedback_nodes(db, 'conductor')) == 1
    asyncio.run(recovery.run())
    assert len(feedback_nodes(db, 'conductor')) == 1
    assert not recovery._pending()


def test_partial_failure_keeps_successful_documents_and_retries_only_missing_work(data, monkeypatch):
    db, document, comment = data
    first = document('first.md')
    second = document('second.md')
    comment(first)
    comment(second)
    original = recovery._reconcile
    monkeypatch.setattr(recovery, 'BATCH_SIZE', 1)
    with monkeypatch.context() as patch:
        def fail_one(artifact):
            if artifact == first:
                raise RuntimeError('one document failed')
            return original(artifact)
        patch.setattr(recovery, '_reconcile', fail_one)
        run(db)
    assert recovery._pending()
    assert len(feedback_nodes(db, 'conductor')) == 1
    asyncio.run(recovery.run())
    assert len(feedback_nodes(db, 'conductor')) == 2
    assert not recovery._pending()


def test_unaddressable_conductor_retries_instead_of_losing_recovery(data, monkeypatch):
    from doceditor import feedback_delivery

    db, document, comment = data
    comment(document())
    with monkeypatch.context() as patch:
        patch.setattr(feedback_delivery, 'recipient_problem', lambda *_: 'owner_unaddressable')
        run(db)
    assert recovery._pending()
    assert not feedback_nodes(db, 'conductor')
    asyncio.run(recovery.run())
    assert len(feedback_nodes(db, 'conductor')) == 1
    assert not recovery._pending()


def test_archived_designated_conductor_counts_as_missing(data, caplog):
    db, document, comment = data
    comment(document())
    db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='conductor'")
    db.commit()
    caplog.set_level('INFO', logger=recovery.log.name)
    run(db)
    assert 'skipped_missing_conductor=1' in caplog.text
    assert not feedback_nodes(db, 'conductor')
