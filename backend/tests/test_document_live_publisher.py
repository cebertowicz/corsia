"""Live registration catches up without rollback or duplicate owner events."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from doceditor.live_store import LiveStore, PendingWrite
from doceditor.live_publication_record import Publication
from doceditor.live_text import create
from registry import service
from registry.identity_metadata import parse_metadata
from registry.shared_documents import resolve_shared_target
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


def owed(store: LiveStore) -> list[int]:
    """Every owed row's sequence; production reads only the oldest row."""
    with closing(sqlite3.connect(store.path)) as db:
        return [row[0] for row in db.execute("SELECT seq FROM publish ORDER BY seq")]


def owe(scope, store, before, after):
    path = scope["category"] / "live.md"
    target = resolve_shared_target(path, workspace=scope["workspace"], thread_id=scope["conductor"],
                                   project_id="project-alpha", description=None, direct_user=False,
                                   proposed_content=after.encode(), db=scope["db"], setup=False,
                                   promoted_hash=hashlib.sha256(before.encode()).hexdigest())
    decision = Publication.decision_for(path, target)
    prior = create(before).get_update()
    new = create(after).get_update()
    record = PendingWrite(after, new, new, hashlib.sha256(before.encode()).hexdigest(),
                          hashlib.sha256(after.encode()).hexdigest(),
                          publication=Publication(scope["conductor"], None, None, prior, decision))
    store.record_pending(record)
    path.write_text(after)
    store.promote(record.op_id)


def setup(scope, tmp_path):
    _write(scope, "live.md", "old")
    store = LiveStore(tmp_path / "live.sqlite")
    store.initialize(create("old").get_update(), hashlib.sha256(b"old").hexdigest())
    return store


def events(scope):
    return scope["db"].execute("SELECT metadata FROM artifact_registration_events"
                               " WHERE event_type='content_edited_by_non_owner' AND source_path=?"
                               " ORDER BY created_at,id", (str(scope["category"] / "live.md"),)).fetchall()


def test_refused_publication_keeps_file_and_live_journal_then_retries_once(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_publisher import publish_one
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "new")
    row = store.next_publication()
    real = service.register_write_strict
    def refused(*args, **kwargs):
        raise PermissionError("registration refused")
    monkeypatch.setattr(service, "register_write_strict", refused)
    with pytest.raises(PermissionError):
        publish_one(store, row, db=scope["db"])
    assert scope["category"].joinpath("live.md").read_text() == "new"
    assert store.next_publication() == row
    assert events(scope) == []
    monkeypatch.setattr(service, "register_write_strict", real)
    publish_one(store, row, db=scope["db"])
    assert store.next_publication() is None
    publish_one(store, row, db=scope["db"])
    assert len(events(scope)) == 1


def test_two_rows_use_stored_event_bytes_and_stamp_survives_delete_refusal(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_publisher import publish_one
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "first")
    owe(scope, store, "first", "second")
    first = store.next_publication()
    publish_one(store, first, db=scope["db"])
    second = store.next_publication()
    real_delete = store.delete_publication
    def refused(seq):
        raise PermissionError("delete refused")
    monkeypatch.setattr(store, "delete_publication", refused)
    with pytest.raises(PermissionError):
        publish_one(store, second, db=scope["db"])
    assert len(events(scope)) == 2
    monkeypatch.setattr(store, "delete_publication", real_delete)
    publish_one(store, second, db=scope["db"])
    publish_one(store, first, db=scope["db"])
    assert len(events(scope)) == 2
    artifact = service.find_file_identity(str(scope["category"] / "live.md"), db=scope["db"])
    assert parse_metadata(artifact["metadata"])["last_written_sha256"] == hashlib.sha256(b"second").hexdigest()
    import json
    diffs = [json.loads(row[0])["diff"] for row in events(scope)]
    assert any("-old" in diff and "+first" in diff for diff in diffs)
    assert any("-first" in diff and "+second" in diff for diff in diffs)


def test_registration_prepared_before_publish_cannot_regress_deleted_row_hash(ownership_scope, tmp_path):
    from doceditor.live_publisher import publish_one
    from registry.write_preparation import prepare_file_write
    scope = ownership_scope
    store = setup(scope, tmp_path)
    path = scope["category"] / "live.md"
    stale = prepare_file_write(str(path), scope["owner"], db=scope["db"])
    owe(scope, store, "old", "new")
    publish_one(store, store.next_publication(), db=scope["db"])
    assert store.next_publication() is None
    service.register_write_strict(scope["owner"], stale, db=scope["db"])
    artifact = service.find_file_identity(str(path), db=scope["db"])
    assert parse_metadata(artifact["metadata"])["last_written_sha256"] == hashlib.sha256(b"new").hexdigest()
    assert len(events(scope)) == 1


def test_owner_change_during_preparation_retains_row_then_targets_current_owner(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher
    from doceditor.feedback_edit_preparation import FeedbackPreparationChanged
    from registry.ownership import rehome_artifact
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "new")
    real = live_publisher.prepare_owner_report
    def prepared_then_rehomed(*args, **kwargs):
        result = real(*args, **kwargs)
        artifact = args[1]
        rehome_artifact(integration=artifact["integration"], type=artifact["type"],
                        external_id=artifact["external_id"], new_owner_thread_id=scope["conductor"],
                        actor_thread_id=scope["owner"], authority="owner",
                        reason="handoff", source_path=str(scope["category"] / "live.md"), db=scope["db"])
        return result
    monkeypatch.setattr(live_publisher, "prepare_owner_report", prepared_then_rehomed)
    with pytest.raises(FeedbackPreparationChanged):
        live_publisher.publish_one(store, store.next_publication(), db=scope["db"])
    assert len(owed(store)) == 1 and events(scope) == []
    monkeypatch.setattr(live_publisher, "prepare_owner_report", real)
    live_publisher.publish_one(store, store.next_publication(), db=scope["db"])
    recipients = scope["db"].execute("SELECT recipient_id FROM doc_feedback_batches WHERE item_count>0").fetchall()
    assert [row[0] for row in recipients] == [scope["conductor"]]
