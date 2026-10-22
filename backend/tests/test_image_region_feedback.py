"""DCOL-FR13.8/13.12: a delivered batch carries region text and the marked-up image."""

from __future__ import annotations

import io
import re
import sqlite3
import uuid
from pathlib import Path

import pytest
from PIL import Image

import infra.database as db_mod
from doc_feedback_helpers import batches, occupy_owner, seed_thread
from doceditor import comments, feedback_batches, image_regions
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


def test_a_persons_regions_are_delivered_in_the_image_owners_project_colour(isolated_db, tmp_path):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,accent_color,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("proj-red", "Red", "#ff0000", "2026-09-29T00:00:00+00:00", "2026-09-29T00:00:00+00:00"),
        )
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET project_id='proj-red' WHERE id='owner'")
        artifact_id, _path = _image_artifact(db, "owner", tmp_path)
        with write_transaction(db, scope="test_region_comment") as connection:
            row, created = comments.create_with_status(
                artifact_id, body="The header wraps.", author="you", author_kind="user",
                region=(0.1, 0.2, 0.3, 0.1), image_rev="rev-1", client_id=uuid.uuid4().hex[:12], db=connection,
            )
            feedback_batches.record_comment(connection, row, created=created)

        description = _node_description(db)
    finally:
        db.close()

    match = re.search(r"Marked-up image \(numbered boxes\): (\S+\.png)", description)
    assert match, description
    with Image.open(match.group(1)) as img:
        # The box's left edge, at x 10% of 800 px, is drawn in the project's accent.
        edge = [img.convert("RGB").getpixel((x, 125)) for x in range(74, 88)]
    assert any(r > 200 and g < 80 and b < 80 for r, g, b in edge), edge


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


def _writer_free() -> bool:
    """Whether another connection could take the SQLite writer right now."""
    other = sqlite3.connect(str(db_mod.DB_PATH), timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


@pytest.fixture
def renders(monkeypatch) -> list[bool]:
    """Record, for each marked-up copy drawn, whether the writer was free while it drew."""
    seen: list[bool] = []
    real = image_regions.render_marked_image

    def spy(data, marks):
        seen.append(_writer_free())
        return real(data, marks)

    monkeypatch.setattr(image_regions, "render_marked_image", spy)
    return seen


def _seeded_regions(db, tmp_path) -> None:
    seed_thread(db, "owner")
    seed_thread(db, "author", title="Reviewer")
    artifact_id, _path = _image_artifact(db, "owner", tmp_path)
    _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")


@pytest.mark.parametrize("close", ["close_batch", "close_due", "turn_end"])
def test_the_copy_is_drawn_after_delivery_commits_never_under_the_writer(isolated_db, tmp_path, renders, close):
    db = get_db()
    try:
        _seeded_regions(db, tmp_path)
        if close == "close_batch":
            feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        elif close == "close_due":
            feedback_batches.close_due("2999-01-01T00:00:00+00:00", db=db)
        else:
            # The Turn's terminal write: delivery nests inside a transaction it does not own.
            with write_transaction(db, scope="test_record_turn_end") as connection:
                feedback_batches.close_for_turn_end(connection, "author")
                assert renders == []
        description = _node_description(db)
    finally:
        db.close()

    assert renders == [True]
    match = re.search(r"Marked-up image \(numbered boxes\): (\S+\.png)", description)
    assert match, description
    assert Path(match.group(1)).is_file()


def test_a_rolled_back_delivery_draws_no_copy(isolated_db, tmp_path, renders):
    db = get_db()
    try:
        _seeded_regions(db, tmp_path)
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope="test_record_turn_end") as connection:
                feedback_batches.close_for_turn_end(connection, "author")
                raise RuntimeError("the terminal write failed")
        assert [row["state"] for row in batches(db)] == ["open"]
    finally:
        db.close()

    assert renders == []


@pytest.mark.parametrize(("step", "error"), [
    ("render_marked_image", ValueError("image is too large to mark up (9000x9000)")),
    ("_save", PermissionError("the working tray is not writable")),
])
def test_a_copy_that_fails_to_draw_after_commit_tells_the_reader(isolated_db, tmp_path, monkeypatch, step, error):
    def broken(*_args):
        raise error

    monkeypatch.setattr(image_regions, step, broken)
    db = get_db()
    try:
        _seeded_regions(db, tmp_path)
        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        named = re.search(r"Marked-up image \(numbered boxes\): (\S+\.png)", _node_description(db))
        notes = db.execute(
            """SELECT summary FROM context_change_journal
               WHERE object_kind='thread_context' AND object_id='owner'"""
        ).fetchall()
    finally:
        db.close()

    assert named and not Path(named.group(1)).exists()
    assert [row["summary"] for row in notes if Path(named.group(1)).name in row["summary"]] == [
        f"The marked-up image {Path(named.group(1)).name} named in your feedback task could not be drawn "
        f"({error}); the region lines in the task still say where each comment is."
    ]


def test_the_marked_up_image_leads_the_wake_text(isolated_db, tmp_path):
    db = get_db()
    try:
        _seeded_regions(db, tmp_path)
        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        description = _node_description(db)
    finally:
        db.close()

    assert description.index("Marked-up image (numbered boxes)") < description.index("Comment ")


def _regional_item() -> dict:
    return {"kind": "comment", "id": "c1", "region_number": 1, "region_x": 0.1, "region_y": 0.2,
            "region_w": 0.3, "region_h": 0.1, "image_rev": "rev-1", "author_kind": "agent",
            "author_thread_id": "author", "artifact_id": "a1"}


def test_an_artifact_with_no_file_says_so(isolated_db, renders):
    db = get_db()
    try:
        items = [_regional_item()]
        image_regions.annotate_delivery(db, items, path=None, reader_thread_id="owner", batch_id="b1")
    finally:
        db.close()

    assert items[0]["marked_image_line"] == "Marked-up image unavailable: the artifact has no image file."
    assert renders == []


def test_an_image_pillow_cannot_decode_names_no_copy(isolated_db, tmp_path, renders):
    svg = tmp_path / "diagram.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    db = get_db()
    try:
        items = [_regional_item()]
        image_regions.annotate_delivery(db, items, path=str(svg), reader_thread_id="owner", batch_id="b1")
    finally:
        db.close()

    assert items[0]["marked_image_line"] == "Marked-up image unavailable: the image file could not be read."
    assert items[0]["region_line"].startswith("Region 1: box at x 10%")
    assert renders == []


def test_a_reader_tray_that_cannot_be_resolved_still_delivers_the_text(isolated_db, tmp_path, monkeypatch, renders):
    # A symlinked or non-directory working tray makes the path resolver raise; delivery must not depend on it.
    def unresolvable(*_args):
        raise ValueError("the working tray is a symlink")

    monkeypatch.setattr(image_regions, "_working_tray", unresolvable)
    image = tmp_path / "shot.png"
    Image.new("RGB", (80, 50), (245, 245, 245)).save(image, format="PNG")
    db = get_db()
    try:
        items = [_regional_item()]
        image_regions.annotate_delivery(db, items, path=str(image), reader_thread_id="owner", batch_id="b1")
    finally:
        db.close()

    assert items[0]["marked_image_line"] == (
        "Marked-up image unavailable: the reader's working folder could not be used (the working tray is a symlink)."
    )
    assert items[0]["region_line"].startswith("Region 1: box at x 10%")
    assert renders == []


def test_a_second_person_region_comment_joins_the_unstarted_task_with_both_region_lines(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, "owner")
        occupy_owner(db, "owner")
        artifact_id, _path = _image_artifact(db, "owner", tmp_path)
        for x, body in ((0.1, "The header wraps."), (0.6, "The icon reads as disabled.")):
            with write_transaction(db, scope="test_person_region_comment") as connection:
                row, created = comments.create_with_status(
                    artifact_id, body=body, author="Steve", author_kind="user", author_thread_id=None,
                    region=(x, 0.2, 0.0, 0.0), image_rev="rev-1", client_id=uuid.uuid4().hex[:12], db=connection,
                )
                feedback_batches.record_comment(connection, row, created=created)
        assert [row["state"] for row in batches(db)] == ["delivered"]

        description = _node_description(db)
        assert "Region 1: point at x 10%" in description and "Region 2: point at x 60%" in description
        assert len(re.findall(r"Marked-up image \(numbered boxes\): \S+-2\.png", description)) == 1
    finally:
        db.close()
