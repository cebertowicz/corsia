"""Connector writes preserve the declared owner and their model projection."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest


def _read(path, sql):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(sql).fetchall()


def _writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.05)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


@pytest.fixture
def catalog(isolated_db):
    # This disposable scenario starts before either harness has discovered models.
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute("DELETE FROM models WHERE harness IN ('claude-code', 'codex')")
        db.commit()
    return isolated_db


def _change(operation, *, db=None):
    from connections.models import set_connection_status, upsert_connection

    if operation == "upsert":
        return upsert_connection(
            type="cli", provider="codex", status="needs_auth",
            config={"binary": "test-codex"}, db=db,
        )
    return set_connection_status("codex", "needs_auth", db=db)


@pytest.mark.parametrize("operation", ["upsert", "status"])
def test_standalone_connector_write_preserves_unknown_owner(catalog, operation):
    from infra.database import TransactionOwnershipError, get_db

    db = get_db()
    db.execute("UPDATE connections SET config='{}' WHERE provider='claude-code'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _change(operation)
        assert db.in_transaction
        assert _read(catalog, "SELECT config FROM connections WHERE provider='claude-code'") == [(None,)]
        db.commit()
        assert _read(catalog, "SELECT config FROM connections WHERE provider='claude-code'") == [("{}",)]
        assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("operation", ["upsert", "status"])
def test_outer_rollback_reverts_connector_and_projection(catalog, operation):
    from infra.database import write_transaction

    with pytest.raises(ValueError, match="abort outer"):
        with write_transaction(None, scope="test_connection_owner") as db:
            _change(operation, db=db)
            raise ValueError("abort outer")
    assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
    assert _read(catalog, "SELECT id FROM models WHERE harness='codex'") == []
    _writer_available(catalog)


@pytest.mark.parametrize("operation", ["upsert", "status"])
def test_projection_collision_preserves_outer_work(catalog, operation):
    from infra.database import write_transaction

    with closing(sqlite3.connect(str(catalog))) as db:
        db.execute(
            "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
            "display_name,created_at,updated_at) VALUES "
            "('codex/gpt-5-codex','other','foreign','foreign','Keep me','t','t')"
        )
        db.commit()
    with write_transaction(None, scope="test_connection_owner") as db:
        db.execute("UPDATE connections SET config='{}' WHERE provider='claude-code'")
        with pytest.raises(ValueError, match="legacy fallback collision"):
            _change(operation, db=db)
        assert db.in_transaction
        assert db.execute("SELECT config FROM connections WHERE provider='claude-code'").fetchone()[0] == "{}"
    assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
    assert _read(catalog, "SELECT display_name FROM models WHERE id='codex/gpt-5-codex'") == [("Keep me",)]
    assert _read(catalog, "SELECT config FROM connections WHERE provider='claude-code'") == [("{}",)]


@pytest.mark.parametrize("operation", ["upsert", "status"])
def test_failed_connector_commit_closes_its_connection(catalog, monkeypatch, operation):
    from connections import models
    from infra import database

    class FailedCommit(sqlite3.Connection):
        closed = False

        def commit(self):
            raise sqlite3.OperationalError("forced connector commit failure")

        def close(self):
            self.closed = True
            super().close()

    db = sqlite3.connect(str(catalog), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(models, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="forced connector commit failure"):
            _change(operation)
        assert db.closed
        assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
        assert _read(catalog, "SELECT id FROM models WHERE harness='codex'") == []
        _writer_available(catalog)
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["upsert", "status"])
def test_standalone_connector_commits_its_projection_before_return(catalog, operation):
    row = _change(operation)
    assert (row["id"], row["type"], row["provider"], row["status"]) == (
        "test-codex", "cli", "codex", "needs_auth",
    )
    assert _read(catalog,
        "SELECT id,source,enabled,available FROM models WHERE harness='codex'"
    ) == [("codex/gpt-5-codex", "legacy_fallback", 1, 0)]
    _writer_available(catalog)


@pytest.mark.parametrize("provider,selector", [("codex", "gpt-5-codex"), ("claude-code", "opus")])
def test_available_connector_keeps_its_referenced_model(catalog, provider, selector):
    from connections.models import set_connection_status

    model_id = f"{provider}/{selector}"
    set_connection_status(provider, "needs_auth")
    with closing(sqlite3.connect(str(catalog))) as db:
        db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('agents',?,'t')", (
            json.dumps({"defaults": {"reviewer_model": model_id}}),
        ))
        db.commit()
    row = set_connection_status(provider, "available")
    assert row["status"] == "available"
    with closing(sqlite3.connect(str(catalog))) as db:
        assert db.execute(
            "SELECT id,source,enabled,available FROM models WHERE id=?", (model_id,),
        ).fetchall() == [(model_id, "reference", 0, 0)]


def test_startup_connector_probes_run_outside_writes(catalog, monkeypatch):
    import shutil
    import connections

    observed = []

    def probe(name):
        _writer_available(catalog)
        observed.append(name)
        return f"/test/{name}"

    monkeypatch.setattr(shutil, "which", probe)
    monkeypatch.setattr(connections, "_gh_status", lambda _binary: (bool(probe("gh-auth")), None))
    monkeypatch.setattr(connections, "_codex_sign_in_state", lambda binary: "ready" if probe("codex-auth") else "needs_auth")
    assert connections.seed_builtin_connections() == 3
    assert observed == ["claude", "gh", "gh-auth", "codex", "codex-auth"]
    assert _read(catalog, "SELECT provider,status FROM connections ORDER BY provider") == [
        ("claude-code", "available"), ("codex", "available"), ("github", "available"),
    ]
