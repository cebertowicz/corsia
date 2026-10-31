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


def _change(*, db=None):
    from connections.models import upsert_connection

    return upsert_connection(
        type="cli", provider="codex", status="needs_auth",
        config={"binary": "test-codex"}, db=db,
    )


def test_standalone_connector_write_preserves_unknown_owner(catalog):
    from infra.database import TransactionOwnershipError, get_db

    db = get_db()
    db.execute("UPDATE connections SET config='{}' WHERE provider='claude-code'")
    try:
        with pytest.raises(TransactionOwnershipError):
            _change()
        assert db.in_transaction
        assert _read(catalog, "SELECT config FROM connections WHERE provider='claude-code'") == [(None,)]
        db.commit()
        assert _read(catalog, "SELECT config FROM connections WHERE provider='claude-code'") == [("{}",)]
        assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
    finally:
        db.rollback()
        db.close()


def test_outer_rollback_reverts_connector_and_projection(catalog):
    from infra.database import write_transaction

    with pytest.raises(ValueError, match="abort outer"):
        with write_transaction(None, scope="test_connection_owner") as db:
            _change(db=db)
            raise ValueError("abort outer")
    assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
    assert _read(catalog, "SELECT id FROM models WHERE harness='codex'") == []
    _writer_available(catalog)


def test_projection_collision_preserves_outer_work(catalog):
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
            _change(db=db)
        assert db.in_transaction
        assert db.execute("SELECT config FROM connections WHERE provider='claude-code'").fetchone()[0] == "{}"
    assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
    assert _read(catalog, "SELECT display_name FROM models WHERE id='codex/gpt-5-codex'") == [("Keep me",)]
    assert _read(catalog, "SELECT config FROM connections WHERE provider='claude-code'") == [("{}",)]


def test_failed_connector_commit_closes_its_connection(catalog, monkeypatch):
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
            _change()
        assert db.closed
        assert _read(catalog, "SELECT status FROM connections WHERE provider='codex'") == [("available",)]
        assert _read(catalog, "SELECT id FROM models WHERE harness='codex'") == []
        _writer_available(catalog)
    finally:
        db.close()


def test_standalone_connector_commits_its_projection_before_return(catalog):
    row = _change()
    assert (row["id"], row["type"], row["provider"], row["status"]) == (
        "test-codex", "cli", "codex", "needs_auth",
    )
    assert _read(catalog,
        "SELECT id,source,enabled,available FROM models WHERE harness='codex'"
    ) == [("codex/gpt-5-codex", "legacy_fallback", 1, 0)]
    _writer_available(catalog)


@pytest.mark.parametrize("provider,selector", [("codex", "gpt-5-codex"), ("claude-code", "opus")])
def test_discovered_connector_keeps_its_referenced_model(catalog, provider, selector):
    from connections.harness_model_rows import CACHE_KEY
    from connections.models import upsert_connection

    model_id = f"{provider}/{selector}"
    config = {"binary": f"test-{provider}"}
    upsert_connection(type="cli", provider=provider, status="needs_auth", config=config)
    with closing(sqlite3.connect(str(catalog))) as db:
        db.execute("INSERT INTO settings (key,value,updated_at) VALUES ('agents',?,'t')", (
            json.dumps({"defaults": {"reviewer_model": model_id}}),
        ))
        db.commit()
    row = upsert_connection(type="cli", provider=provider, status="available",
                            config={**config, CACHE_KEY: {}})
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
    assert connections.seed_builtin_connections() == 3
    assert observed == ["claude", "gh", "gh-auth", "codex"]
    assert _read(catalog, "SELECT provider,status FROM connections ORDER BY provider") == [
        ("claude-code", "available"), ("codex", "available"), ("github", "available"),
    ]
