"""Migrated sentinel-addressed feedback follows ordinary close and retry behavior."""

from auth_fixtures import OWNER_USER
from doc_feedback_helpers import add_comment, feedback_nodes, seed_document, seed_thread
from doceditor import feedback_batches, feedback_comments
from infra.database import get_db
from registry.identity_metadata import merge_identity_metadata


def test_migrated_comment_collision_delivers_once_and_reverse_preserves_it(isolated_db, monkeypatch):
    from doceditor import feedback_state
    from infra.migration_202_to_203_feedback_recipients import migrate_202_to_203, reverse_203_to_202

    db = get_db()
    try:
        artifact = seed_document(db, 'project:example')
        monkeypatch.setattr(feedback_comments, 'comment_recipients', lambda *_a, **_kw: ('project:example',))
        comment, feedback = add_comment(db, artifact, author_kind='user', author_id=None, author=OWNER_USER,
                                        delivery_policy='document-held')
        source = feedback['batch_ids'][0]
        db.execute("""INSERT INTO doc_feedback_batches
            (id,artifact_id,author_kind,author_id,recipient_id,delivery_policy,state,opened_at,last_item_at,
             closes_at,item_count,created_at,updated_at)
            SELECT 'collision',artifact_id,author_kind,author_id,'ui',delivery_policy,state,opened_at,last_item_at,
                   closes_at,item_count,created_at,updated_at FROM doc_feedback_batches WHERE id=?""", (source,))
        db.execute("INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES ('collision',?)", (comment['id'],))
        db.execute("UPDATE doc_feedback_batch_items SET event='reopened',event_at='1',event_by='ui' WHERE batch_id=?", (source,))
        db.commit()
        add_comment(db, artifact, author_kind='user', author_id=None, author=OWNER_USER, delivery_policy='document-held')
        before = feedback_state.open_counts_for_document(db, artifact)
        assert before['document_held_count'] == 2
        migrate_202_to_203(db)
        assert feedback_state.open_counts_for_document(db, artifact) == before
        assert db.execute('SELECT 1 FROM doc_feedback_batches WHERE id=?', (source,)).fetchone() is None
        db.commit()
        feedback_batches.close_due('9999-01-01T00:00:00+00:00', db=db)
        reverse_203_to_202(db)
        assert tuple(db.execute("SELECT state,recipient_id,delivery_outcome FROM doc_feedback_batches WHERE id='collision'").fetchone()) == ('delivered', 'ui', 'ui')
        assert db.execute('SELECT COUNT(*) FROM doc_feedback_batches').fetchone()[0] == 1
    finally:
        db.close()


def test_migrated_edit_collision_coalesces_one_edit_and_delivers_on_deadline(isolated_db, monkeypatch):
    from doceditor import feedback_state
    from infra.migration_202_to_203_feedback_recipients import migrate_202_to_203

    db = get_db()
    try:
        artifact = seed_document(db, 'project:example')
        monkeypatch.setattr(feedback_comments, 'comment_recipients', lambda *_a, **_kw: ('project:example',))
        _, feedback = add_comment(db, artifact, author_kind='user', author_id=None, author=OWNER_USER,
                                  delivery_policy='document-held')
        source = feedback['batch_ids'][0]
        db.execute("""INSERT INTO doc_feedback_batches
            (id,artifact_id,author_kind,author_id,recipient_id,delivery_policy,state,opened_at,last_item_at,
             closes_at,item_count,created_at,updated_at)
            SELECT 'collision',artifact_id,author_kind,author_id,'ui',delivery_policy,state,opened_at,last_item_at,
                   closes_at,1,created_at,updated_at FROM doc_feedback_batches WHERE id=?""", (source,))
        db.executemany("INSERT INTO doc_feedback_edit_items VALUES (?,'Person','diff','a','old','b','new',1,'1','2')",
                       [(source,), ('collision',)])
        db.execute('UPDATE doc_feedback_batches SET item_count=2 WHERE id=?', (source,))
        db.commit()
        before = feedback_state.open_counts_for_document(db, artifact)
        timing = tuple(db.execute('SELECT opened_at,last_item_at,closes_at FROM doc_feedback_batches WHERE id=?', (source,)).fetchone())
        migrate_202_to_203(db)
        assert feedback_state.open_counts_for_document(db, artifact)['document_held_count'] == before['document_held_count'] - 1
        assert tuple(db.execute("SELECT opened_at,last_item_at,closes_at FROM doc_feedback_batches WHERE id='collision'").fetchone()) == timing
        assert db.execute('SELECT 1 FROM doc_feedback_batches WHERE id=?', (source,)).fetchone() is None
        db.commit()
        assert not feedback_batches._close_due_batch(db, 'collision', '0001-01-01T00:00:00+00:00')[0]
        assert feedback_batches._close_due_batch(db, 'collision', '9999-01-01T00:00:00+00:00')[0]
        assert tuple(db.execute("SELECT state,recipient_id,delivery_outcome FROM doc_feedback_batches WHERE id='collision'").fetchone()) == ('delivered', 'ui', 'ui')
        assert db.execute('SELECT COUNT(*) FROM doc_feedback_edit_items').fetchone()[0] == 0
    finally:
        db.close()


def test_dev_era_project_owner_creates_conductor_and_sentinel_batches(isolated_db, monkeypatch):
    from registry import document_supervisor as legacy
    from registry.shared_principals import artifact_owner

    db = get_db()
    try:
        seed_thread(db, 'supervisor')
        artifact = seed_document(db, 'project:example')
        db.commit()
        monkeypatch.setattr(legacy, '_scope', lambda *_args: ('example', 'supervisor'))

        def dev_recipients(db, document, *, workspace):
            # The dev-era resolver returned the header supervisor and literal owner.
            owner = artifact_owner(str(document['id']), db=db)
            row, _ = legacy._supervisor(db, owner, legacy.stored_pick(document),
                                        lambda: legacy._scope(db, document, workspace)[1])
            return tuple(dict.fromkeys(str(who) for who in (row['id'] if row else None, owner) if who))

        monkeypatch.setattr(feedback_comments, 'comment_recipients', dev_recipients)
        _, feedback = add_comment(db, artifact, author_kind='user', author_id=None, author=OWNER_USER)
        recipients = {db.execute('SELECT recipient_id FROM doc_feedback_batches WHERE id=?', (batch,)).fetchone()[0]
                      for batch in feedback['batch_ids']}
        assert recipients == {'supervisor', 'project:example'}
    finally:
        db.close()


def test_legacy_open_batch_delivers_to_person(isolated_db, monkeypatch):
    db = get_db()
    try:
        artifact = seed_document(db, 'project:example')
        db.commit()
        monkeypatch.setattr(feedback_comments, 'comment_recipients', lambda *_a, **_kw: ('project:example',))
        _, feedback = add_comment(db, artifact, author_kind='user', author_id=None, author=OWNER_USER)
        batch = feedback['batch_ids'][0]
        from infra.migration_202_to_203_feedback_recipients import migrate_202_to_203
        migrate_202_to_203(db)
        db.commit()
        feedback_batches.close_batch(db, batch, 'send')
        assert db.execute('SELECT state,delivery_outcome FROM doc_feedback_batches WHERE id=?', (batch,)).fetchone()[:] == ('delivered', 'ui')
    finally:
        db.close()


def test_old_project_batches_retry_without_duplicate_supervisor_delivery(isolated_db, monkeypatch):
    db = get_db()
    try:
        seed_thread(db, "supervisor")
        artifact = seed_document(db, "project:example")
        merge_identity_metadata(db, artifact, {"supervisor_thread_id": "supervisor"})
        db.commit()
        actual = feedback_comments.comment_recipients
        monkeypatch.setattr(feedback_comments, "comment_recipients", lambda *_a, **_kw: ("supervisor", "project:example"))
        old_batches = []
        for index in range(17):
            for _ in range(3 if index < 3 else 2):
                _, feedback = add_comment(db, artifact, author_kind="user", author_id=None, author=OWNER_USER,
                                          delivery_policy="document-held")
            ids = feedback["batch_ids"]
            supervisor = next(i for i in ids if db.execute("SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (i,)).fetchone()[0] == "supervisor")
            sentinel = next(i for i in ids if i != supervisor)
            feedback_batches.close_batch(db, supervisor, "send")
            # Represent the pre-fallback delivery result persisted by an earlier version.
            db.execute("UPDATE doc_feedback_batches SET state='undelivered', undelivered_reason='owner_unavailable' WHERE id=?", (sentinel,))
            db.commit()
            assert db.execute("SELECT state,delivery_outcome FROM doc_feedback_batches WHERE id=?", (supervisor,)).fetchone()[:] == ("delivered", "node")
            assert db.execute("SELECT state,undelivered_reason FROM doc_feedback_batches WHERE id=?", (sentinel,)).fetchone()[:] == ("undelivered", "owner_unavailable")
            old_batches.append(sentinel)
        monkeypatch.setattr(feedback_comments, "comment_recipients", actual)
        before = len(feedback_nodes(db, "supervisor"))
        from infra.migration_202_to_203_feedback_recipients import migrate_202_to_203
        migrate_202_to_203(db)
        db.commit()
        for batch in old_batches:
            assert feedback_batches.retry_batch(db, batch)[0]
        rows = [db.execute("SELECT state, undelivered_reason, recipient_id FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()
                for batch in old_batches]
        assert all(tuple(row) == ("delivered", None, "ui") for row in rows)
        assert len(feedback_nodes(db, "supervisor")) == before
    finally:
        db.close()


def test_migrated_undelivered_batch_retries_to_conductor_once(isolated_db, monkeypatch):
    from infra.migration_202_to_203_feedback_recipients import migrate_202_to_203, reverse_203_to_202
    db = get_db()
    try:
        seed_thread(db, 'conductor')
        db.execute("INSERT INTO projects(id,name,conductor_thread_id,created_at,updated_at) VALUES ('example','Example','conductor','1','1')")
        artifact = seed_document(db, 'conductor')
        monkeypatch.setattr(feedback_comments, 'comment_recipients', lambda *_a, **_kw: ('project:example',))
        _, feedback = add_comment(db, artifact, author_kind='user', author_id=None, author=OWNER_USER)
        batch = feedback['batch_ids'][0]
        db.execute("UPDATE doc_feedback_batches SET state='undelivered',attempts=2,undelivered_reason='owner_unavailable' WHERE id=?", (batch,))
        migrate_202_to_203(db)
        db.commit()
        assert feedback_batches.retry_batch(db, batch)[0]
        reverse_203_to_202(db)
        assert tuple(db.execute('SELECT state,recipient_id,attempts,delivery_outcome,undelivered_reason FROM doc_feedback_batches WHERE id=?', (batch,)).fetchone()) == ('delivered', 'conductor', 3, 'node', None)
        assert not feedback_batches.retry_batch(db, batch)[0]
        assert len(feedback_nodes(db, 'conductor')) == 1
    finally:
        db.close()
