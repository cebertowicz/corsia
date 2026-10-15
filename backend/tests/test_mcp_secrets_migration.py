"""The MCP secrets migration moves credential values into the key store.

The claims: each credential-named env and header value lands in the store and
leaves the config; every other value is untouched; a second start changes
nothing; a failure after the store write loses nothing and leaves only
unreferenced values the next sweep removes; a server holding a value the store
cannot take is left whole. Values resolve through the migrated database's rows.
Every value here is synthetic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing

import pytest

from infra import key_store, key_store_values
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_mcp_secrets import FROM_VERSION, migrate_mcp_secrets
from infra.schema import SCHEMA_VERSION
from tools import mcp_secrets

SERVERS = {
    "local": {
        "command": "fixture",
        "args": ["--flag"],
        "env": {"SERVICE_TOKEN": "synthetic-token-0001", "LOG_LEVEL": "debug", "DB_PASSWORD": "synthetic-pass-0002"},
    },
    "remote": {
        "url": "http://127.0.0.1:1/mcp",
        "headers": {"X-Api-Key": "synthetic-key-0003", "X-Org": "acme"},
    },
    "plain": {"command": "fixture", "env": {"LOG_LEVEL": "info"}},
}


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return root


def _seed(path, servers=SERVERS) -> None:
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('tools', ?, 't')",
            (json.dumps({"web": {"search": {"count": 3}}, "mcpServers": servers}),),
        )
        db.execute(f"PRAGMA user_version={FROM_VERSION}")
        db.commit()


def _tools(db: sqlite3.Connection) -> dict:
    return json.loads(db.execute("SELECT value FROM settings WHERE key = 'tools'").fetchone()[0])


def _file_keys() -> set[str]:
    return set(json.loads((key_store.keys_dir() / key_store.VALUES_FILE).read_text()))


def _live_refs(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT value_ref FROM key_store_entries WHERE value_ref IS NOT NULL")}


def _resolve(path, server: str, config: dict) -> tuple[dict, dict]:
    with closing(sqlite3.connect(path)) as db:
        return mcp_secrets.resolve(server, config, db=db)


def _clear_backups(directory) -> None:
    for backup in directory.glob("*.bak"):
        backup.unlink()


def _migrate(path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        assert migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_step_is_registered_and_current():
    assert SCHEMA_VERSION == FROM_VERSION + 8
    assert MIGRATIONS[FROM_VERSION] is migrate_mcp_secrets
    assert FROM_VERSION - 1 in MIGRATIONS  # the ladder has no gap below it


def test_credential_values_move_and_everything_else_is_untouched(state, tmp_path, caplog):
    path = tmp_path / "mcp.db"
    _seed(path)
    caplog.set_level(logging.INFO)

    _migrate(path)

    with closing(sqlite3.connect(path)) as db:
        tools = _tools(db)
        assert tools["web"] == {"search": {"count": 3}}
        servers = tools["mcpServers"]
        assert "synthetic" not in json.dumps(servers)
        assert servers["local"]["env"] == {"LOG_LEVEL": "debug"}
        assert servers["local"]["args"] == ["--flag"]
        assert servers["remote"]["headers"] == {"X-Org": "acme"}
        assert servers["plain"] == SERVERS["plain"]
        rows = {r[0] for r in db.execute("SELECT name FROM key_store_entries WHERE set_at IS NOT NULL")}

    env, _ = _resolve(path, "local", servers["local"])
    assert env == {"SERVICE_TOKEN": "synthetic-token-0001", "DB_PASSWORD": "synthetic-pass-0002"}
    _, headers = _resolve(path, "remote", servers["remote"])
    assert headers == {"X-Api-Key": "synthetic-key-0003"}
    expected = (mcp_secrets.referenced_entries("local", servers["local"])
                | mcp_secrets.referenced_entries("remote", servers["remote"]))
    assert len(rows) == 3 and rows == expected

    messages = [record.getMessage() for record in caplog.records]
    assert any("moved 3 credential value(s) from 2 server(s)" in message for message in messages)
    assert "synthetic" not in "\n".join(messages)


def test_a_second_run_of_the_step_changes_nothing(state, tmp_path):
    path = tmp_path / "again.db"
    _seed(path)
    _migrate(path)
    values_before = (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes()

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        settings_before = [tuple(r) for r in db.execute("SELECT key, value FROM settings ORDER BY key")]
        db.execute(f"PRAGMA user_version={FROM_VERSION}")
        db.commit()
        _clear_backups(tmp_path)
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        assert [tuple(r) for r in db.execute("SELECT key, value FROM settings ORDER BY key")] == settings_before
    assert (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes() == values_before


def test_a_failure_after_the_store_write_loses_nothing_on_the_next_start(state, tmp_path, monkeypatch):
    path = tmp_path / "crash.db"
    _seed(path)
    real = key_store_values.store_missing

    def write_then_fail(conn, values, now):
        real(conn, values, now)
        raise RuntimeError("simulated failure between the store write and the config cleanup")

    monkeypatch.setattr(key_store_values, "store_missing", write_then_fail)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(MigrationError):
            migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == FROM_VERSION
        assert _tools(db)["mcpServers"] == SERVERS  # every inline copy still there
        # The rows rolled back with it: the values written are unreferenced leftovers.
        assert not _live_refs(db) and len(_file_keys()) == 3

    monkeypatch.setattr(key_store_values, "store_missing", real)
    _clear_backups(tmp_path)
    _migrate(path)
    with closing(sqlite3.connect(path)) as db:
        servers = _tools(db)["mcpServers"]
    assert "synthetic" not in json.dumps(servers)
    env, _ = _resolve(path, "local", servers["local"])
    assert env["SERVICE_TOKEN"] == "synthetic-token-0001"
    with closing(sqlite3.connect(path)) as db:
        assert _file_keys() == _live_refs(db)  # the retry's sweep removed the leftovers


def test_a_server_with_a_value_the_store_cannot_take_is_left_whole(state, tmp_path):
    path = tmp_path / "odd.db"
    odd = {"command": "fixture", "env": {"SERVICE_TOKEN": "has a space synthetic-0004", "API_KEY": "synthetic-key-0005"}}
    _seed(path, {"odd": odd, "local": SERVERS["local"]})

    _migrate(path)

    with closing(sqlite3.connect(path)) as db:
        servers = _tools(db)["mcpServers"]
    assert servers["odd"] == odd
    assert "synthetic" not in json.dumps(servers["local"])
    assert len(_file_keys()) == 2  # only the sound server's values


def test_a_database_with_no_mcp_servers_is_untouched(state, tmp_path):
    path = tmp_path / "empty.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(f"PRAGMA user_version={FROM_VERSION}")
        db.commit()
    _migrate(path)
    assert not (key_store.keys_dir() / key_store.VALUES_FILE).exists()
