"""Superseded save copies are deleted only after committed reference checks."""

import pytest

from infra import config
from infra.database import get_db, write_transaction
from plans.approval_records import record_approval
from registry import models
from tests.registry_helpers import make_session


def test_rollback_discard_preserves_copy_reused_by_another_approval(isolated_db, tmp_path, monkeypatch):
    import hashlib
    from plans.approval_copies import prepare

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    db = get_db()
    try:
        thread = make_session(db, prefix="approval-shared")
        path = tmp_path / "shared.md"
        path.write_text("shared")
        artifact = models.register_artifact(integration="workspace", type="document", external_id=str(path),
                                            parent_thread_id=thread, title="Shared", db=db)
        prepared = prepare(path, b"shared", None, db=db)
        copy = prepared.created[0]
        with write_transaction(db, scope="test_shared_commit"):
            record_approval(db, artifact_id=artifact["id"], node_id=None, source="bar", source_id="hold",
                            doc_sha256=hashlib.sha256(b"shared").hexdigest(), images=[])
        prepared.discard()
        assert copy.read_bytes() == b"shared"
    finally:
        db.close()


def test_cleanup_waits_for_commit_and_preserves_shared_images(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    db = get_db()
    try:
        thread = make_session(db, prefix="approval-cleanup")
        path = tmp_path / "design.md"
        path.write_text("design")
        artifact = models.register_artifact(integration="workspace", type="document", external_id=str(path), parent_thread_id=thread, title="Design", db=db)
        root = tmp_path / ".corsia" / "approved"
        root.mkdir(parents=True)
        page = root / ("a" * 16 + ".md")
        frame = root / ("b" * 16 + ".png")
        page.write_bytes(b"old page")
        frame.write_bytes(b"old frame")
        with write_transaction(db, scope="test_approval_setup"):
            record_approval(db, artifact_id=artifact["id"], node_id=None, source="save", source_id=None,
                            doc_sha256="a" * 64, images=[{"path": str(tmp_path / "frame.png"), "sha256": "b" * 64}])
            record_approval(db, artifact_id=artifact["id"], node_id=None, source="bar", source_id="h",
                            doc_sha256="c" * 64, images=[{"path": str(tmp_path / "frame.png"), "sha256": "b" * 64}])
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope="test_approval_rollback"):
                record_approval(db, artifact_id=artifact["id"], node_id=None, source="save", source_id=None,
                                doc_sha256="d" * 64, images=[])
                assert page.exists() and frame.exists()
                raise RuntimeError("rollback")
        assert page.exists() and frame.exists()
        with write_transaction(db, scope="test_approval_commit"):
            record_approval(db, artifact_id=artifact["id"], node_id=None, source="save", source_id=None,
                            doc_sha256="d" * 64, images=[])
            assert page.exists() and frame.exists()
        assert not page.exists()
        assert frame.exists()
    finally:
        db.close()
