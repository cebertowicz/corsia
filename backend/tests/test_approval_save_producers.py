"""The existing save producers create versions only after an approval exists."""

import hashlib

import pytest

from doceditor.document_save import DocumentSave, _write_document
from doceditor.live_publication_record import Publication
from doceditor.live_publisher import register_publication
from doceditor.live_text import create
from plans.approval_records import record_approval
from registry import service
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


def _rows(db, artifact_id):
    return db.execute("SELECT source,doc_sha256 FROM artifact_approvals WHERE artifact_id=? ORDER BY rowid", (artifact_id,)).fetchall()


def test_plain_save_records_only_for_an_approved_document(ownership_scope):
    scope = ownership_scope
    path = scope["root"] / ".threads" / scope["owner"] / "approval-save.md"
    body = DocumentSave(str(path), "first", project_id="project-alpha", thread_id=scope["owner"], create=True)
    _write_document(body, path, direct_user=True)
    artifact = service.find_file_identity(str(path), db=scope["db"])
    assert _rows(scope["db"], artifact["id"]) == []
    record_approval(scope["db"], artifact_id=artifact["id"], node_id=None, source="bar", source_id="hold",
                    doc_sha256=hashlib.sha256(b"first").hexdigest(), images=[])
    scope["db"].commit()
    _write_document(DocumentSave(str(path), "second", project_id="project-alpha", thread_id=scope["owner"]), path, direct_user=True)
    assert [(row["source"], row["doc_sha256"]) for row in _rows(scope["db"], artifact["id"])] == [
        ("bar", hashlib.sha256(b"first").hexdigest()), ("save", hashlib.sha256(b"second").hexdigest())]


def test_live_publication_records_only_for_an_approved_document(ownership_scope):
    scope = ownership_scope
    path = scope["root"] / ".threads" / scope["owner"] / "live-approval.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("first")
    service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    artifact = service.find_file_identity(str(path), db=scope["db"])
    publication = Publication(scope["owner"], None, None, create("first").get_update(),
                              Publication.decision_for(path, None), direct_user=True, project_id="project-alpha")
    path.write_text("second")
    register_publication(publication, "second", hashlib.sha256(b"second").hexdigest(), stamp=None, db=scope["db"])
    assert _rows(scope["db"], artifact["id"]) == []
    record_approval(scope["db"], artifact_id=artifact["id"], node_id=None, source="bar", source_id="hold",
                    doc_sha256=hashlib.sha256(b"second").hexdigest(), images=[])
    scope["db"].commit()
    path.write_text("third")
    register_publication(publication, "third", hashlib.sha256(b"third").hexdigest(), stamp=None, db=scope["db"])
    assert [(row["source"], row["doc_sha256"]) for row in _rows(scope["db"], artifact["id"])] == [
        ("bar", hashlib.sha256(b"second").hexdigest()), ("save", hashlib.sha256(b"third").hexdigest())]


@pytest.mark.parametrize("producer", ["plain", "durable", "live"])
@pytest.mark.parametrize("second_change", [False, True])
def test_approval_arrives_during_save(ownership_scope, monkeypatch, producer, second_change):
    from plans import public

    scope = ownership_scope
    path = (scope["category"] / "race.md" if producer == "durable" else
            scope["root"] / ".threads" / scope["owner"] / "race.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    if producer == "durable":
        _write(scope, "race.md", "before")
    else:
        path.write_text("before")
        service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    artifact = service.find_file_identity(str(path), db=scope["db"])
    scope["db"].commit()
    original = public.prepare_save_approval
    calls = []

    def prepare_then_change(*args, db, **kwargs):
        result = original(*args, db=db, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            record_approval(db, artifact_id=artifact["id"], node_id=None, source="bar", source_id="hold",
                            doc_sha256=hashlib.sha256(b"before").hexdigest(), images=[])
            db.commit()
        elif second_change:
            db.execute("DELETE FROM artifact_approvals WHERE artifact_id=?", (artifact["id"],))
            db.commit()
        return result

    monkeypatch.setattr(public, "prepare_save_approval", prepare_then_change)
    reports = []
    if producer == "plain":
        from doceditor import document_save
        monkeypatch.setattr(document_save, "_report", lambda decision, path, before, actor, *args, **kwargs: reports.append(before))
    if producer in {"plain", "durable"}:
        def save():
            return _write_document(DocumentSave(str(path), "after", project_id="project-alpha",
                                                thread_id=scope["owner"]), path, direct_user=True,
                                   expected_sha256=hashlib.sha256(b"before").hexdigest())
    else:
        publication = Publication(scope["owner"], None, None, create("before").get_update(),
                                  Publication.decision_for(path, None), direct_user=True, project_id="project-alpha")
        path.write_text("after")

        def save():
            return register_publication(publication, "after", hashlib.sha256(b"after").hexdigest(),
                                        stamp=None, db=scope["db"])

    if second_change:
        from doceditor.document_save_errors import DocumentSaveError
        from plans.approval_records import ApprovalChangedDuringSave

        with pytest.raises(DocumentSaveError if producer != "live" else ApprovalChangedDuringSave,
                           match="Approval changed during save"):
            save()
        assert _rows(scope["db"], artifact["id"]) == []
    else:
        save()
        assert [row["source"] for row in _rows(scope["db"], artifact["id"])] == ["bar", "save"]
        if producer == "plain":
            assert reports == [b"before"]
    assert len(calls) == 2


@pytest.mark.parametrize("producer", ["plain", "live"])
def test_approval_disappears_during_save(ownership_scope, monkeypatch, producer):
    from plans import public

    scope = ownership_scope
    path = scope["root"] / ".threads" / scope["owner"] / "disappears.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("before")
    service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    artifact = service.find_file_identity(str(path), db=scope["db"])
    record_approval(scope["db"], artifact_id=artifact["id"], node_id=None, source="bar", source_id="hold",
                    doc_sha256=hashlib.sha256(b"before").hexdigest(), images=[])
    scope["db"].commit()
    original = public.prepare_save_approval
    created = []

    def prepare_then_delete(*args, db, **kwargs):
        result = original(*args, db=db, **kwargs)
        created.extend(result.created)
        db.execute("DELETE FROM artifact_approvals WHERE artifact_id=?", (artifact["id"],))
        db.commit()
        return result

    monkeypatch.setattr(public, "prepare_save_approval", prepare_then_delete)
    if producer == "plain":
        _write_document(DocumentSave(str(path), "after", project_id="project-alpha", thread_id=scope["owner"]),
                        path, direct_user=True)
    else:
        publication = Publication(scope["owner"], None, None, create("before").get_update(),
                                  Publication.decision_for(path, None), direct_user=True, project_id="project-alpha")
        path.write_text("after")
        register_publication(publication, "after", hashlib.sha256(b"after").hexdigest(), stamp=None, db=scope["db"])
    assert _rows(scope["db"], artifact["id"]) == []
    assert created and all(not copy.exists() for copy in created)
