"""Old sentinel-addressed feedback is repaired when it is next attempted."""

from auth_fixtures import OWNER_USER
from doc_feedback_helpers import add_comment, feedback_nodes, seed_document, seed_thread
from doceditor import feedback_batches, feedback_comments
from infra.database import get_db
from registry.identity_metadata import merge_identity_metadata


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
        for batch in old_batches:
            assert feedback_batches.retry_batch(db, batch)[0]
        rows = db.execute("SELECT state, undelivered_reason FROM doc_feedback_batches WHERE recipient_id='project:example'").fetchall()
        assert len(rows) == 17
        assert all(row[0] == "delivered" and row[1] is None for row in rows)
        assert len(feedback_nodes(db, "supervisor")) == before
    finally:
        db.close()
