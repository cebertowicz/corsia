"""Scale proof for artifact reconcile's healthy-file path."""
from __future__ import annotations

from infra.database import get_db
from registry import models, reconcile
from tests.registry_helpers import make_session


def test_apply_reads_receipts_once_for_many_healthy_candidates(tmp_path):
    db = get_db()
    thread_id = make_session(db, prefix="reconcile-scale")
    historical = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=str(tmp_path / "already-repaired.md"),
        parent_thread_id=thread_id,
        title="already-repaired.md",
        path=str(tmp_path / "already-repaired.md"),
        db=db,
    )
    reconcile.apply_for_thread(thread_id)
    for index in range(40):
        path = tmp_path / f"artifact-{index}.md"
        path.write_text("present")
        models.register_artifact(
            integration="workspace",
            type="document",
            external_id=str(path),
            parent_thread_id=thread_id,
            title=path.name,
            path=str(path),
            db=db,
        )

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        result = reconcile.apply_for_thread(thread_id)
    finally:
        db.set_trace_callback(None)

    receipt_reads = [
        statement for statement in statements
        if "FROM artifact_reconcile_receipts" in statement
        and statement.lstrip().upper().startswith("SELECT")
    ]
    assert result["counts"] == {"applied": 0, "skipped": 0, "failed": 0}
    assert len(receipt_reads) == 1
    assert "artifact_id IN" in receipt_reads[0]
    assert historical["id"] not in receipt_reads[0]
