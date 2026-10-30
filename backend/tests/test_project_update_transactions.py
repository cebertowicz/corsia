"""Project mutation checks and writes share one owner, excluding filesystem work."""

from contextlib import closing
import sqlite3

import pytest

from api import projects
from infra import config, database
from auth_fixtures import OWNER_TOKEN

TOKEN = OWNER_TOKEN


@pytest.fixture
def project_db(isolated_db):
    with database.write_transaction(None, scope="project_transaction_test_seed") as db:
        db.executemany(
            "INSERT INTO projects (id,name,status,created_at,updated_at) VALUES (?,?,?,'t','t')",
            [("project", "Original", "active"), ("other", "Other", "active")],
        )
    return isolated_db


def _stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute("SELECT id,name,status FROM projects ORDER BY id").fetchall()


def _writer_locked(path):
    with closing(sqlite3.connect(str(path), timeout=0)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            assert "locked" in str(error)
            return True
        db.rollback()
        return False


def _change(client, operation, *, body=None, project_id="project", token=TOKEN):
    if operation == "update":
        return client.patch(
            f"/api/projects/{project_id}?token={token}",
            json={"name": "Renamed"} if body is None else body,
        )
    return client.post(
        f"/api/projects/{project_id}/transition?token={token}",
        json={"to_status": "archived"} if body is None else body,
    )


@pytest.mark.parametrize("operation", ["update", "transition"])
def test_project_validation_excludes_a_competing_writer(
    project_db, integration_client, monkeypatch, operation,
):
    real_get_project = projects._get_project
    protected = []

    def observed(project_id, *, db):
        if not protected:
            protected.append(_writer_locked(project_db))
        return real_get_project(project_id, db=db)

    monkeypatch.setattr(projects, "_get_project", observed)
    response = _change(integration_client, operation)

    assert response.status_code == 200, response.text
    assert protected == [True]
    assert _stored(project_db) == [
        ("other", "Other", "active"),
        ("project", "Renamed" if operation == "update" else "Original",
         "active" if operation == "update" else "archived"),
    ]
    assert not _writer_locked(project_db)


@pytest.mark.parametrize("operation", ["update", "transition"])
def test_failed_project_commit_leaves_no_partial_update(
    project_db, integration_client, monkeypatch, operation,
):
    real_get_db = database.get_db
    before = _stored(project_db)

    class FailCommit(database._NoOpClose):
        def commit(self):
            raise RuntimeError("project commit rejected")

    def failing_db():
        return FailCommit(real_get_db())

    monkeypatch.setattr(database, "get_db", failing_db)
    monkeypatch.setattr(projects, "get_db", failing_db)
    with pytest.raises(RuntimeError, match="project commit rejected"):
        _change(integration_client, operation)
    assert _stored(project_db) == before
    assert not _writer_locked(project_db)


@pytest.mark.parametrize("operation", ["update", "transition"])
def test_failed_project_sql_leaves_no_partial_update(project_db, integration_client, operation):
    before = _stored(project_db)
    with database.write_transaction(None, scope="project_transaction_test_fault") as db:
        db.execute(
            "CREATE TRIGGER reject_project_update AFTER UPDATE ON projects "
            "BEGIN SELECT RAISE(FAIL, 'project write rejected'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="project write rejected"):
        _change(integration_client, operation)
    assert _stored(project_db) == before
    assert not _writer_locked(project_db)


@pytest.mark.parametrize("operation", ["update", "transition"])
def test_projection_failure_does_not_undo_the_committed_update(
    project_db, integration_client, monkeypatch, operation,
):
    def projection(_project, *, db):
        assert not db.in_transaction
        assert not _writer_locked(project_db)
        raise ValueError("project path projection failed")

    monkeypatch.setattr(projects, "_project_api_projection", projection)
    with pytest.raises(ValueError, match="project path projection failed"):
        _change(integration_client, operation)
    assert _stored(project_db) == [
        ("other", "Other", "active"),
        ("project", "Renamed" if operation == "update" else "Original",
         "active" if operation == "update" else "archived"),
    ]
    assert not _writer_locked(project_db)


def test_project_path_preparation_and_projection_run_without_a_writer(
    project_db, integration_client, monkeypatch,
):
    root = (config.WORKSPACE / "projects" / "project-root").resolve()
    root.mkdir(parents=True)
    real_roots = projects.compute_doc_roots
    real_projection = projects._project_api_projection
    observed = []

    def roots(path):
        observed.append(("prepare", _writer_locked(project_db)))
        return real_roots(path)

    def projection(project, *, db):
        observed.append(("project", _writer_locked(project_db)))
        return real_projection(project, db=db)

    monkeypatch.setattr(projects, "compute_doc_roots", roots)
    monkeypatch.setattr(projects, "_project_api_projection", projection)
    response = _change(integration_client, "update", body={"path": str(root)})

    assert response.status_code == 200, response.text
    assert response.json()["project"]["path"] == str(root)
    assert observed == [("prepare", False), ("project", False)]


@pytest.mark.parametrize(
    "operation,body,project_id,token,status",
    [
        ("update", {"name": "Other"}, "project", TOKEN, 409),
        ("update", {"name": "New"}, "missing", TOKEN, 404),
        ("update", {}, "project", TOKEN, 400),
        ("update", {"accent_color": "invalid"}, "project", TOKEN, 400),
        ("update", {"description": "changed"}, "project", "invalid", 401),
        ("transition", {"to_status": "exploring"}, "project", TOKEN, 400),
        ("transition", {"to_status": "archived"}, "missing", TOKEN, 404),
        ("transition", {"to_status": "invalid"}, "project", TOKEN, 400),
        ("transition", {"to_status": "archived"}, "project", "invalid", 401),
    ],
)
def test_project_refusals_preserve_rows_and_release_the_writer(
    project_db, integration_client, operation, body, project_id, token, status,
):
    before = _stored(project_db)
    response = _change(
        integration_client, operation, body=body, project_id=project_id, token=token,
    )
    assert response.status_code == status, response.text
    assert _stored(project_db) == before
    assert not _writer_locked(project_db)


def test_integration_client_does_not_start_usage_rollup_writer(integration_client):
    """The session app must not refresh mutable rollups across per-test databases."""
    from api import usage_rollup

    assert integration_client.application_loop not in usage_rollup._refreshing
