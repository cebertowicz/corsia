"""DCOL-FR13.8/13.12: a delivered batch carries region text and the marked-up image."""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

from PIL import Image

from doc_feedback_helpers import batches, seed_thread
from doceditor import comments, feedback_batches
from infra.database import get_db, write_transaction
from plans.feedback_nodes import PROVENANCE
from registry import models as registry_models


def _image_artifact(db, owner: str, directory: Path) -> tuple[str, Path]:
    path = directory / f"screen-{uuid.uuid4().hex[:8]}.png"
    out = io.BytesIO()
    Image.new("RGB", (800, 500), (245, 245, 245)).save(out, format="PNG")
    path.write_bytes(out.getvalue())
    artifact = registry_models.register_artifact(
        integration="local", type="image", external_id=str(path), parent_thread_id=owner,
        title=path.name, path=str(path), metadata={"zone": "workspace"}, db=db,
    )
    db.commit()
    return str(artifact["id"]), path


def _region(db, artifact_id: str, *, x: float, y: float, w: float = 0.0, h: float = 0.0, body: str) -> dict:
    with write_transaction(db, scope="test_region_comment") as connection:
        row, created = comments.create_with_status(
            artifact_id, body=body, author="Reviewer", author_kind="agent", author_thread_id="author",
            region=(x, y, w, h), image_rev="rev-1", client_id=uuid.uuid4().hex[:12], db=connection,
        )
        feedback_batches.record_comment(connection, row, created=created)
    return row


def _node_description(db) -> str:
    row = db.execute(
        """SELECT n.description FROM plan_nodes n JOIN plan_node_events e ON e.node_id=n.id
           WHERE e.provenance=? AND e.event='node_created'""",
        (PROVENANCE,),
    ).fetchone()
    return str(row["description"])


def test_delivered_batch_names_each_region_and_carries_the_marked_up_image(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id, _path = _image_artifact(db, "owner", tmp_path)
        _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")
        _region(db, artifact_id, x=0.7, y=0.6, body="Wrong icon.")

        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        description = _node_description(db)
    finally:
        db.close()

    assert "Region 1: box at x 10%, y 20%, 30% wide, 10% tall" in description
    assert "Region 2: point at x 70%, y 60%" in description
    assert "Feedback: The header wraps." in description
    match = re.search(r"Marked-up image \(numbered boxes\): (\S+\.png)", description)
    assert match, description
    rendered = Path(match.group(1))
    assert rendered.is_file()
    assert "/.threads/owner/working/" in str(rendered)
    with Image.open(rendered) as img:
        assert img.size == (800, 500)


def test_a_missing_image_still_delivers_the_region_text(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id, path = _image_artifact(db, "owner", tmp_path)
        _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")
        path.unlink()

        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        description = _node_description(db)
    finally:
        db.close()

    assert "Region 1: box at x 10%, y 20%, 30% wide, 10% tall" in description
    assert "Marked-up image unavailable" in description
