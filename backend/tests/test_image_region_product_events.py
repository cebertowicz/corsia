"""EAS33: region comments report placement, moves and marked-copy failures, never text or paths."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from doc_feedback_helpers import batches, seed_thread
from doceditor import comments, feedback_batches, image_regions
from infra.database import get_db, write_transaction
from test_image_region_feedback import _image_artifact, _region


@pytest.fixture
def seen(monkeypatch):
    from doceditor import live_events
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_events, "emit", lambda action, **properties: events.append((action, properties)))
    return events


def _seed(db, tmp_path) -> tuple[str, Path]:
    seed_thread(db, "owner")
    seed_thread(db, "author", title="Reviewer")
    return _image_artifact(db, "owner", tmp_path)


def _private(events, *secrets) -> bool:
    payload = json.dumps([properties for _, properties in events])
    return not any(secret in payload for secret in secrets)


def test_placing_a_region_reports_its_shape_once(isolated_db, tmp_path, seen):
    db = get_db()
    try:
        artifact_id, path = _seed(db, tmp_path)
        box = _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")
        _region(db, artifact_id, x=0.5, y=0.5, body="This icon is blurry.")
        with write_transaction(db, scope="test_replay") as connection:
            comments.create_with_status(
                artifact_id, body="The header wraps.", author="Reviewer", author_kind="agent",
                author_thread_id="author", region=(0.1, 0.2, 0.3, 0.1), image_rev="rev-1",
                client_id=box["id"], db=connection,
            )
        comments.create(artifact_id, body="A text comment.", anchor_text="x", db=db)
    finally:
        db.close()

    assert seen == [
        ("image_region_comment", {"artifact_id": artifact_id, "outcome": "placed", "shape": "box", "author_kind": "agent"}),
        ("image_region_comment", {"artifact_id": artifact_id, "outcome": "placed", "shape": "point", "author_kind": "agent"}),
    ]
    assert _private(seen, "header", "blurry", str(path))


def test_a_rolled_back_placement_reports_nothing(isolated_db, tmp_path, seen):
    db = get_db()
    try:
        artifact_id, _path = _seed(db, tmp_path)
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope="test_rollback") as connection:
                comments.create(artifact_id, body="Gone.", author_kind="agent", region=(0.1, 0.1, 0.0, 0.0),
                                image_rev="rev-1", client_id=uuid.uuid4().hex[:12], db=connection)
                raise RuntimeError("roll back")
    finally:
        db.close()

    assert seen == []


def test_moving_a_pin_reports_its_new_shape(isolated_db, tmp_path, seen):
    db = get_db()
    try:
        artifact_id, _path = _seed(db, tmp_path)
        row = _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")
        seen.clear()
        comments.update(row["id"], {"region_x": 0.4, "region_y": 0.4, "region_w": 0.0, "region_h": 0.0,
                                    "image_rev": "rev-2"}, db=db)
        comments.update(row["id"], {"resolved": True}, db=db)
    finally:
        db.close()

    assert seen == [("image_region_comment", {"artifact_id": artifact_id, "outcome": "moved", "shape": "point"})]


def test_a_delivered_copy_that_fails_to_draw_is_reported(isolated_db, tmp_path, monkeypatch, seen):
    def broken(*_args):
        raise ValueError(f"cannot decode {tmp_path}")

    monkeypatch.setattr(image_regions, "render_marked_image", broken)
    db = get_db()
    try:
        artifact_id, _path = _seed(db, tmp_path)
        _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")
        seen.clear()
        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
    finally:
        db.close()

    assert seen == [("image_region_marked_copy", {"outcome": "failed", "stage": "delivery", "error": "ValueError"})]
    assert _private(seen, str(tmp_path))


def test_an_agent_result_copy_that_fails_to_draw_is_reported(isolated_db, tmp_path, monkeypatch, seen):
    def broken(*_args):
        raise PermissionError("the working tray is not writable")

    monkeypatch.setattr(image_regions, "_save", broken)
    db = get_db()
    try:
        artifact_id, path = _seed(db, tmp_path)
        row = _region(db, artifact_id, x=0.1, y=0.2, w=0.3, h=0.1, body="The header wraps.")
        seen.clear()
        with pytest.raises(PermissionError):
            image_regions.write_marked_image(db, image=path, rows=[row], reader_thread_id="author", label="shot")
    finally:
        db.close()

    assert seen == [("image_region_marked_copy", {"outcome": "failed", "stage": "agent_result", "error": "PermissionError"})]
