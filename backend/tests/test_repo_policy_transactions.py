"""Repository policy writes respect the declared database owner."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from infra import database, project_repo_policy


@pytest.fixture
def policy(isolated_db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('project','original','t','t')"
        )
        db.execute(
            "INSERT INTO project_repo_policies VALUES ('project','app',?,'main',1)",
            (str(repo),),
        )
        db.commit()
    return isolated_db, repo


def _rows(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT repo_name,pinned_branch,protect_base_checkout "
            "FROM project_repo_policies ORDER BY repo_name"
        ).fetchall()


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.05)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _change(repo, operation, *, db=None):
    if operation == "clear":
        return project_repo_policy.clear_repo_policy("project", "app", db=db)
    return project_repo_policy.register_repo_policy(
        "project", "new" if operation == "insert" else "app", repo,
        pinned_branch=" dev ", protect_base_checkout=False, db=db,
    )


@pytest.mark.parametrize("operation", ["insert", "update", "clear"])
def test_anonymous_policy_write_preserves_unknown_owner(policy, operation):
    path, repo = policy
    db = database.get_db()
    db.execute("UPDATE projects SET name='outer' WHERE id='project'")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            _change(repo, operation)
        assert db.in_transaction
        assert _rows(path) == [("app", "main", 1)]
        db.commit()
        with closing(sqlite3.connect(str(path))) as observer:
            assert observer.execute("SELECT name FROM projects").fetchone() == ("outer",)
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("operation", ["insert", "update", "clear"])
def test_named_policy_write_remains_in_outer_transaction(policy, operation):
    path, repo = policy
    with pytest.raises(ValueError, match="outer aborted"):
        with database.write_transaction(None, scope="test_policy_owner") as db:
            _change(repo, operation, db=db)
            assert _rows(path) == [("app", "main", 1)]
            raise ValueError("outer aborted")
    assert _rows(path) == [("app", "main", 1)]
    _writer_available(path)


@pytest.mark.parametrize("operation,event", [
    ("insert", "INSERT"), ("update", "UPDATE"), ("clear", "DELETE"),
])
def test_statement_partial_failure_rolls_back_only_policy_write(policy, operation, event):
    path, repo = policy
    with closing(sqlite3.connect(str(path))) as db:
        db.execute(
            f"CREATE TRIGGER reject_policy AFTER {event} ON project_repo_policies "
            "BEGIN SELECT RAISE(FAIL, 'policy rejected'); END"
        )
        db.commit()
    with database.write_transaction(None, scope="test_policy_owner") as db:
        db.execute("UPDATE projects SET name='outer' WHERE id='project'")
        with pytest.raises(sqlite3.IntegrityError, match="policy rejected"):
            _change(repo, operation, db=db)
        assert db.in_transaction
        assert [tuple(row) for row in db.execute(
            "SELECT repo_name,pinned_branch,protect_base_checkout FROM project_repo_policies"
        )] == [("app", "main", 1)]
    assert _rows(path) == [("app", "main", 1)]
    with closing(sqlite3.connect(str(path))) as observer:
        assert observer.execute("SELECT name FROM projects").fetchone() == ("outer",)


@pytest.mark.parametrize("operation", ["insert", "update", "clear"])
def test_failed_policy_commit_releases_writer(policy, monkeypatch, operation):
    path, repo = policy

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("policy commit failed")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(path), factory=FailedCommit)
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(project_repo_policy, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="policy commit failed"):
            _change(repo, operation)
        assert db.closed
        assert _rows(path) == [("app", "main", 1)]
        _writer_available(path)
    finally:
        db.close()


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("operation,expected", [
    ("insert", [("app", "main", 1), ("new", "dev", 0)]),
    ("update", [("app", "dev", 0)]),
    ("clear", []),
])
def test_policy_result_is_durable_on_clean_entry(policy, named, operation, expected):
    path, repo = policy
    db = database.get_db() if named else None
    try:
        result = _change(repo, operation, db=db)
        assert _rows(path) == expected
        if operation == "clear":
            assert result is None
        else:
            assert (result.project_id, result.path, result.pinned_branch,
                    result.protect_base_checkout) == ("project", repo, "dev", False)
        _writer_available(path)
    finally:
        if db is not None:
            db.close()


@pytest.mark.parametrize("method", ["resolve", "is_dir"])
def test_path_validation_precedes_policy_writer(policy, monkeypatch, method):
    path, repo = policy
    original = getattr(Path, method)
    observed = []

    def checked(self, *args, **kwargs):
        if self == repo:
            _writer_available(path)
            observed.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, checked)
    _change(repo, "update")
    assert observed == [True]
    assert _rows(path) == [("app", "dev", 0)]
