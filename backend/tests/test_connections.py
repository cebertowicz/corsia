
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class _TestNoOpClose:

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def close(self) -> None:
        try:
            object.__getattribute__(self, "_conn").rollback()
        except sqlite3.Error:
            pass

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


@pytest.fixture
def connections_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS connections (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            provider TEXT NOT NULL,
            config TEXT,
            status TEXT NOT NULL DEFAULT 'available',
            scope TEXT NOT NULL DEFAULT 'global',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider)
        );
        CREATE INDEX IF NOT EXISTS idx_connections_status ON connections(type, status);
        """
    )
    from infra.vendor_account_schema import VENDOR_ACCOUNT_SCHEMA_SQL
    conn.executescript(VENDOR_ACCOUNT_SCHEMA_SQL)
    conn.commit()
    wrapper = _TestNoOpClose(conn)

    from infra import database
    from connections import models as connection_models

    factory = lambda: wrapper  # noqa: E731
    monkeypatch.setattr(database, "get_db", factory)
    monkeypatch.setattr(connection_models, "get_db", factory)

    yield wrapper
    conn.close()


class _FakeTool:
    name = "fake_spawn"
    description = "fake"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):  # pragma: no cover - never called here
        return "ok"



class TestConnectionModels:
    def test_available_false_when_absent(self, connections_db):
        from connections.models import connection_available
        assert connection_available("claude-code", db=connections_db) is False

    def test_available_true_when_available(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, connection_available, upsert_connection,
        )
        upsert_connection(
            type=ConnectionType.CLI, provider="claude-code",
            config={"binary": "claude"}, status=ConnectionStatus.AVAILABLE,
            db=connections_db,
        )
        assert connection_available("claude-code", db=connections_db) is True

    def test_available_false_when_not_installed(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, connection_available, upsert_connection,
        )
        upsert_connection(
            type=ConnectionType.CLI, provider="claude-code",
            status=ConnectionStatus.NOT_INSTALLED, db=connections_db,
        )
        assert connection_available("claude-code", db=connections_db) is False

    def test_available_false_when_needs_auth(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, connection_available, upsert_connection,
        )
        upsert_connection(
            type=ConnectionType.CLI, provider="github",
            status=ConnectionStatus.NEEDS_AUTH, db=connections_db,
        )
        assert connection_available("github", db=connections_db) is False

    def test_upsert_idempotent_on_provider(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, list_connections, upsert_connection,
        )
        upsert_connection(
            type=ConnectionType.CLI, provider="claude-code",
            status=ConnectionStatus.NOT_INSTALLED, db=connections_db,
        )
        upsert_connection(
            type=ConnectionType.CLI, provider="claude-code",
            status=ConnectionStatus.AVAILABLE, db=connections_db,
        )
        rows = list_connections(db=connections_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "available"

    def test_config_roundtrips_as_dict(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, get_connection, upsert_connection,
        )
        cfg = {"binary": "/usr/local/bin/claude", "permission_mode": "bypassPermissions"}
        upsert_connection(
            type=ConnectionType.CLI, provider="claude-code",
            config=cfg, status=ConnectionStatus.AVAILABLE, db=connections_db,
        )
        row = get_connection("claude-code", db=connections_db)
        assert row is not None
        assert row["config"] == cfg
        assert row["type"] == "cli"
        assert row["scope"] == "global"

    def test_get_connection_none_when_absent(self, connections_db):
        from connections.models import get_connection
        assert get_connection("nonexistent", db=connections_db) is None

    def test_list_filters_by_status(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, list_connections, upsert_connection,
        )
        upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                          status=ConnectionStatus.AVAILABLE, db=connections_db)
        upsert_connection(type=ConnectionType.CLI, provider="github",
                          status=ConnectionStatus.NEEDS_AUTH, db=connections_db)
        available = list_connections(status=ConnectionStatus.AVAILABLE, db=connections_db)
        assert len(available) == 1
        assert available[0]["provider"] == "claude-code"

    def test_set_connection_status(self, connections_db):
        from connections.models import (
            ConnectionStatus, ConnectionType, connection_available,
            set_connection_status, upsert_connection,
        )
        upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                          status=ConnectionStatus.AVAILABLE, db=connections_db)
        set_connection_status("claude-code", ConnectionStatus.NEEDS_AUTH, db=connections_db)
        assert connection_available("claude-code", db=connections_db) is False



class TestRegisterIfAvailable:
    def test_registers_when_available(self, connections_db):
        from connections import register_if_available
        from connections.models import ConnectionStatus, ConnectionType, upsert_connection
        from tools.registry import ToolRegistry

        upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                          status=ConnectionStatus.AVAILABLE, db=connections_db)
        reg = ToolRegistry()
        did = register_if_available(reg, "claude-code", lambda: _FakeTool(), db=connections_db)
        assert did is True
        assert reg.has("fake_spawn")

    def test_skips_when_absent(self, connections_db):
        from connections import register_if_available
        from tools.registry import ToolRegistry

        reg = ToolRegistry()
        did = register_if_available(reg, "claude-code", lambda: _FakeTool(), db=connections_db)
        assert did is False
        assert not reg.has("fake_spawn")

    def test_skips_when_not_installed(self, connections_db):
        from connections import register_if_available
        from connections.models import ConnectionStatus, ConnectionType, upsert_connection
        from tools.registry import ToolRegistry

        upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                          status=ConnectionStatus.NOT_INSTALLED, db=connections_db)
        reg = ToolRegistry()
        did = register_if_available(reg, "claude-code", lambda: _FakeTool(), db=connections_db)
        assert did is False



def test_binary_resolver_reports_both_setup_clis_absent_in_empty_search_roots(tmp_path):
    from connections import binary_from_config

    empty_root = tmp_path / "empty-bin"
    empty_root.mkdir()

    assert binary_from_config("claude-code", {}, candidate_roots=(Path(empty_root),)) is None
    assert binary_from_config("codex", {}, candidate_roots=(Path(empty_root),)) is None


class TestSeedBuiltinConnections:
    def test_claude_available_when_on_path(self, connections_db, monkeypatch):
        import shutil
        from connections import seed_builtin_connections
        from connections.models import get_connection

        monkeypatch.setattr(shutil, "which",
                            lambda b: "/usr/local/bin/claude" if b == "claude" else None)
        seed_builtin_connections(db=connections_db)
        row = get_connection("claude-code", db=connections_db)
        assert row is not None
        assert row["status"] == "available"
        assert row["type"] == "cli"
        assert row["config"]["binary"] == "/usr/local/bin/claude"

    def test_claude_not_installed_when_absent(self, connections_db, monkeypatch):
        import os
        import shutil
        from connections import seed_builtin_connections
        from connections.models import get_connection

        monkeypatch.setattr(shutil, "which", lambda b: None)
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        seed_builtin_connections(db=connections_db)
        row = get_connection("claude-code", db=connections_db)
        assert row is not None
        assert row["status"] == "not_installed"

    def test_github_needs_auth_when_present_but_unauthed(self, connections_db, monkeypatch):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import get_connection

        monkeypatch.setattr(shutil, "which",
                            lambda b: "/usr/bin/gh" if b == "gh" else None)
        monkeypatch.setattr(conn_pkg, "_gh_status", lambda _binary: (False, None))
        seed_builtin_connections(db=connections_db)
        row = get_connection("github", db=connections_db)
        assert row is not None
        assert row["status"] == "needs_auth"

    def test_github_available_when_authed(self, connections_db, monkeypatch):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import get_connection

        monkeypatch.setattr(shutil, "which",
                            lambda b: "/usr/bin/gh" if b == "gh" else None)
        monkeypatch.setattr(conn_pkg, "_gh_status", lambda _binary: (True, None))
        seed_builtin_connections(db=connections_db)
        row = get_connection("github", db=connections_db)
        assert row is not None
        assert row["status"] == "available"

    def test_codex_needs_auth_when_present_but_unauthed(self, connections_db, monkeypatch):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import get_connection

        monkeypatch.setattr(shutil, "which",
                            lambda b: "/usr/local/bin/codex" if b == "codex" else None)
        monkeypatch.setattr(conn_pkg, "_codex_sign_in_state", lambda _binary: "needs_auth")
        seed_builtin_connections(db=connections_db)
        row = get_connection("codex", db=connections_db)
        assert row is not None
        assert row["status"] == "needs_auth"

    def test_codex_available_when_authed(self, connections_db, monkeypatch):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import get_connection

        monkeypatch.setattr(shutil, "which",
                            lambda b: "/usr/local/bin/codex" if b == "codex" else None)
        monkeypatch.setattr(conn_pkg, "_codex_sign_in_state", lambda _binary: "ready")
        seed_builtin_connections(db=connections_db)
        row = get_connection("codex", db=connections_db)
        assert row is not None
        assert row["status"] == "available"

    def test_seed_idempotent(self, connections_db, monkeypatch):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import list_connections

        monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(conn_pkg, "_gh_status", lambda _binary: (True, None))
        seed_builtin_connections(db=connections_db)
        seed_builtin_connections(db=connections_db)
        providers = sorted(r["provider"] for r in list_connections(db=connections_db))
        assert providers == ["claude-code", "codex", "github"]

    def test_manual_binary_preserved_when_executable(self, connections_db, monkeypatch, tmp_path):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import (
            ConnectionStatus, ConnectionType, get_connection, upsert_connection,
        )
        manual = tmp_path / "custom-claude"
        manual.write_text("#!/bin/sh\n")
        manual.chmod(0o755)
        upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                          config={"binary": str(manual)},
                          status=ConnectionStatus.AVAILABLE, db=connections_db)
        monkeypatch.setattr(
            shutil, "which",
            lambda b: "/somewhere/else/claude" if b == "claude" else None,
        )
        monkeypatch.setattr(conn_pkg, "_gh_status", lambda _binary: (False, None))
        seed_builtin_connections(db=connections_db)
        row = get_connection("claude-code", db=connections_db)
        assert row["config"]["binary"] == str(manual)
        assert row["status"] == "available"

    def test_stale_manual_binary_replaced(self, connections_db, monkeypatch):
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import (
            ConnectionStatus, ConnectionType, get_connection, upsert_connection,
        )
        upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                          config={"binary": "/does/not/exist"},
                          status=ConnectionStatus.AVAILABLE, db=connections_db)
        monkeypatch.setattr(
            shutil, "which",
            lambda b: "/usr/local/bin/claude" if b == "claude" else None,
        )
        monkeypatch.setattr(conn_pkg, "_gh_status", lambda _binary: (False, None))
        seed_builtin_connections(db=connections_db)
        row = get_connection("claude-code", db=connections_db)
        assert row["config"]["binary"] == "/usr/local/bin/claude"

    def test_discover_falls_back_to_candidate_path_off_PATH(self, connections_db, monkeypatch):
        import os
        import shutil
        import connections as conn_pkg
        from connections import seed_builtin_connections
        from connections.models import get_connection

        cand = os.path.expanduser("~/.local/bin/claude")
        real_isfile, real_access = os.path.isfile, os.access
        monkeypatch.setattr(shutil, "which", lambda b: None)  # nothing on PATH
        monkeypatch.setattr(os.path, "isfile", lambda p: p == cand or real_isfile(p))
        monkeypatch.setattr(os, "access", lambda p, m: p == cand or real_access(p, m))
        monkeypatch.setattr(conn_pkg, "_gh_status", lambda _binary: (False, None))
        seed_builtin_connections(db=connections_db)
        row = get_connection("claude-code", db=connections_db)
        assert row["config"]["binary"] == cand
        assert row["status"] == "available"
