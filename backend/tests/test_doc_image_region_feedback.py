"""DCOL-FR13.8 in a document: a delivered batch carries one marked-up copy per image it comments on."""

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


def _png(path: Path, size: tuple[int, int]) -> Path:
    out = io.BytesIO()
    Image.new("RGB", size, (245, 245, 245)).save(out, format="PNG")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.getvalue())
    return path


def _in_workspace() -> Path:
    """A fresh folder inside the workspace, where a document's images must be for anyone to read them."""
    from infra import config

    return Path(config.WORKSPACE) / f"review-{uuid.uuid4().hex[:8]}"


def _doc_artifact(db, owner: str, directory: Path) -> str:
    path = directory / "notes.md"
    path.write_text("# Notes\n\n![Login](a/shot.png)\n\n![Cart](b/shot.png)\n")
    artifact = registry_models.register_artifact(
        integration="local", type="document", external_id=str(path), parent_thread_id=owner,
        title=path.name, path=str(path), metadata={"zone": "workspace"}, db=db,
    )
    db.commit()
    return str(artifact["id"])


def _region(db, artifact_id: str, image: Path, *, x: float, y: float, body: str, stale: bool = False) -> dict:
    with write_transaction(db, scope="test_region_comment") as connection:
        row, created = comments.create_with_status(
            artifact_id, body=body, author="Reviewer", author_kind="agent", author_thread_id="author",
            anchor_text=f"![x]({image.parent.name}/{image.name})", from_off=0, to_off=1,
            region=(x, y, 0.2, 0.2), image_rev="old" if stale else _sha(image), image_src=str(image),
            client_id=uuid.uuid4().hex[:12], db=connection,
        )
        feedback_batches.record_comment(connection, row, created=created)
    return row


def _sha(path: Path) -> str:
    from registry.identity_metadata import sha256_file

    return sha256_file(path)


def _node_description(db) -> str:
    row = db.execute(
        """SELECT n.description FROM plan_nodes n JOIN plan_node_events e ON e.node_id=n.id
           WHERE e.provenance=? AND e.event='node_created'""",
        (PROVENANCE,),
    ).fetchone()
    return str(row["description"])


def test_a_doc_batch_carries_one_marked_copy_per_image_and_judges_staleness_per_image(isolated_db):
    folder = _in_workspace()
    login = _png(folder / "a" / "shot.png", (800, 500))
    cart = _png(folder / "b" / "shot.png", (300, 200))
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = _doc_artifact(db, "owner", folder)
        _region(db, artifact_id, login, x=0.1, y=0.1, body="Header wraps.")
        _region(db, artifact_id, cart, x=0.5, y=0.5, body="Total cut off.", stale=True)
        _region(db, artifact_id, login, x=0.6, y=0.6, body="Wrong icon.")

        feedback_batches.close_due("2999-01-01T00:00:00+00:00", db=db)
        description = _node_description(db)
    finally:
        db.close()

    copies = re.findall(r"Marked-up image \(numbered boxes\): (\S+\.png)", description)
    assert len(copies) == 2, description
    sizes = set()
    for copy in copies:
        assert Path(copy).is_file(), copy
        with Image.open(copy) as img:
            sizes.add(img.size)
    assert sizes == {(800, 500), (300, 200)}
    assert description.count("Region 1:") == 2 and "Region 2:" in description
    assert description.count("(the image changed after this was placed)") == 1


def test_a_doc_batch_draws_no_copy_of_an_image_now_outside_the_readers_scope(isolated_db, tmp_path_factory, monkeypatch):
    """A link retargeted after the comment was placed hands the reader nothing from outside its scope, nor reads it."""
    from doceditor import image_regions
    folder = _in_workspace()
    login = _png(folder / "a" / "shot.png", (800, 500))
    outside = _png(tmp_path_factory.mktemp("outside-scope") / "secret.png", (300, 200))
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = _doc_artifact(db, "owner", folder)
        _region(db, artifact_id, login, x=0.1, y=0.1, body="Header wraps.")
        login.unlink()
        login.symlink_to(outside)
        read = []
        real = image_regions.sha256_file
        monkeypatch.setattr(image_regions, "sha256_file", lambda path: read.append(Path(path).resolve()) or real(path))

        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        description = _node_description(db)
    finally:
        db.close()

    assert "Marked-up image (numbered boxes)" not in description, description
    assert "Marked-up image unavailable:" in description and "outside" in description, description
    assert outside.resolve() not in read
