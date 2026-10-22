
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from registry import bootload, models  # noqa: E402
from infra.doc_comments_schema import DOCUMENT_SCHEMA_SQL  # noqa: E402
from infra.schema import STRUCTURED_PLAN_SCHEMA_SQL  # noqa: E402
from tests.registry_helpers import ARTIFACT_REGISTRY_DDL  # noqa: E402
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN

@pytest.fixture
def db(tmp_path, monkeypatch):
    from infra import config
    from infra.platform_paths import WorkspaceSelection
    from registry import service

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(
        config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id="bootload-workspace",
            root=tmp_path.resolve(),
            name="Bootload workspace",
        ),
    )
    monkeypatch.setattr(
        service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [tmp_path.resolve()],
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Registration checks project shared/ roots, so it reads projects.
    conn.executescript(
        ARTIFACT_REGISTRY_DDL
        + STRUCTURED_PLAN_SCHEMA_SQL
        + DOCUMENT_SCHEMA_SQL
        + "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT);"
    )
    conn.commit()
    return conn


def _meta(row) -> dict:
    return json.loads(row["metadata"]) if row["metadata"] else {}


def _registered_under(root, db) -> list[dict]:
    """Active workspace documents whose path lies under ``root``."""
    prefix = str(Path(root).resolve())
    rows = []
    for identity in db.execute(
        "SELECT id FROM artifact_identities "
        "WHERE integration='workspace' AND type='document' AND state='active' "
        "ORDER BY external_id"
    ).fetchall():
        row = models.get_artifact(identity["id"], db=db)
        if row is not None and row["state"] == "active" and str(row["path"]).startswith(prefix):
            rows.append(row)
    return rows


def test_sync_creates_flagged_rows_with_sentinel(db, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    (ws / "BRIEF.md").write_text("b")
    (ws / "PROJECT.md").write_text("p")

    bootload.sync_bootload_documents("p1", ws, ["PROJECT.md", "BRIEF.md"], db=db)

    rows = _registered_under(ws, db)
    assert {r["title"] for r in rows} == {"PROJECT.md", "BRIEF.md"}
    for r in rows:
        assert r["parent_thread_id"] == "project:p1"
        assert "bootload" not in _meta(r), "no designation metadata is written"
        assert r["type"] == "document" and r["integration"] == "workspace"
        assert r["external_id"].startswith("workspace://bootload-workspace/")
        assert Path(r["path"]).name == r["title"]


def test_existing_thread_owned_row_keeps_owner(db, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    f = ws / "PROJECT.md"; f.write_text("p")
    ext = str(f.resolve())

    legacy = models.register_artifact(
        integration="workspace", type="document", external_id=ext,
        parent_thread_id="session-abc", title="PROJECT.md", path=ext, db=db,
    )
    bootload.sync_bootload_documents("p1", ws, ["PROJECT.md"], db=db)

    from registry import service

    row = service.resolve_file_identity(ext, db=db)
    assert row["id"] == legacy["id"]
    assert row["external_id"].startswith("workspace://bootload-workspace/")
    assert row["parent_thread_id"] == "session-abc", "registration must not re-home"
    assert "bootload" not in _meta(row)


def test_sync_returns_failure_instead_of_swallowing(db, tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()
    (ws / "PROJECT.md").write_text("p")
    outside = tmp_path.parent / "outside-root"

    assert bootload.sync_bootload_documents("p1", ws, ["PROJECT.md"], db=db) is None
    failure = bootload.sync_bootload_documents("p1", outside, ["PROJECT.md"], db=db)
    assert isinstance(failure, Exception)


def test_report_sync_failure_logs_and_counts(monkeypatch):
    from loguru import logger

    events = []
    monkeypatch.setattr(
        bootload, "emit_product_event", lambda *a, **k: events.append((a, k))
    )
    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        bootload.report_sync_failure("p1", ValueError("boom"), source="project_create")
    finally:
        logger.remove(sink)

    assert any("bootload_registry_sync_failed project_id=p1" in str(x) for x in lines)
    assert events == [
        (
            ("bootload", "registry_sync_failed"),
            {"properties": {"project_id": "p1", "source": "project_create", "error": "ValueError"}},
        )
    ]


def test_update_metadata_merge_and_delete(db, tmp_path):
    ext = str(tmp_path / "x.md")
    models.register_artifact(
        integration="workspace", type="document", external_id=ext,
        parent_thread_id="t1", metadata={"keep": 1}, db=db,
    )
    models.update_metadata("workspace", "document", ext, {"bootload": {"level": "project"}}, db=db)
    row = models.resolve("workspace", "document", ext, db=db)
    assert _meta(row) == {"keep": 1, "bootload": {"level": "project"}}

    models.update_metadata("workspace", "document", ext, {"bootload": None}, db=db)
    assert _meta(models.resolve("workspace", "document", ext, db=db)) == {"keep": 1}

    assert models.update_metadata("workspace", "document", "nope", {"a": 1}, db=db) is None



@pytest.mark.integration
class TestProjectFilesSyncRegistry:
    def test_directory_context_create_mirrors_into_rows(self, integration_client):
        name = f"breg-{uuid.uuid4().hex[:8]}"
        r = integration_client.post(f"/api/projects?token={_TOKEN}", json={"name": name})
        assert r.status_code == 200, r.text
        p = r.json()["project"]

        from infra.database import get_db
        db = get_db()
        try:
            rows = _registered_under(Path(p["path"]) / "context", db)
            assert {x["title"] for x in rows} == {
                "PROJECT.md",
                "COORDINATION-MODEL.md",
            }, "create seeds rows"
            assert all(x["external_id"].startswith("workspace://") for x in rows)
            assert {x["path"] for x in rows} == {
                str((Path(p["path"]) / "context" / "PROJECT.md").resolve()),
                str(
                    (
                        Path(p["path"])
                        / "context"
                        / "COORDINATION-MODEL.md"
                    ).resolve()
                ),
            }

            created = integration_client.post(
                f"/api/context/bootloader/file?token={_TOKEN}",
                json={
                    "scope": "project",
                    "project_id": p["id"],
                    "path": "NOTES.md",
                    "content": "notes",
                },
            )
            assert created.status_code == 200, created.text
            rows = _registered_under(Path(p["path"]) / "context", db)
            assert {x["title"] for x in rows} == {
                "PROJECT.md",
                "COORDINATION-MODEL.md",
                "NOTES.md",
            }
        finally:
            db.close()
