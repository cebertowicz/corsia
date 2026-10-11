"""Schema v108 moves every External and oMLX server key into the key store.

The claims: a live server's key lands as ``server-<id>`` and leaves its metadata
(other metadata survives); a removed server's key is dropped, not migrated; a
value already in the store wins; a second start changes nothing; a failure
after the store write loses nothing on the next start; and a database without
the servers table passes. Keys resolve through the migrated database's rows.
Every value here is synthetic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing

import pytest

from infra import key_store
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.key_store_servers import resolve_server
from infra.migration_107_to_108_server_keys import migrate_107_to_108
from infra.migration_109_to_110_agent_use_restriction import migrate_109_to_110
from infra.migration_mcp_secrets import migrate_mcp_secrets
from infra.schema import SCHEMA_VERSION

FIELD = "api_key"


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return root


def _server(db: sqlite3.Connection, server_id: str, meta: object, status: str = "online") -> None:
    raw = meta if isinstance(meta, str) or meta is None else json.dumps(meta)
    db.execute(
        "INSERT INTO network_servers (id, provider, kind, instance_id, instance_name, port, status,"
        " metadata_json, last_seen_at, created_at, updated_at) VALUES (?, 'external', 'external', ?, ?, 0, ?,"
        " ?, 't', 't', 't')",
        (server_id, server_id, server_id, status, raw),
    )


def _meta(db: sqlite3.Connection, server_id: str) -> dict:
    return json.loads(db.execute("SELECT metadata_json FROM network_servers WHERE id = ?", (server_id,)).fetchone()[0])


def _seed(path) -> None:
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _server(db, "ns-live1", {FIELD: "live-key-0001", "base_url": "https://example.test/v1"})
        _server(db, "ns-live2", {FIELD: "live-key-0002"})
        _server(db, "ns-blank", {FIELD: "  "})
        _server(db, "ns-none", {"base_url": "https://example.test/v1"})
        _server(db, "ns-bad", "{not json")
        _server(db, "ns-gone", {FIELD: "gone-key-0003"}, status="removed")
        db.execute("PRAGMA user_version=107")
        db.commit()


def _resolve(path, server_id: str) -> str | None:
    with closing(sqlite3.connect(path)) as db:
        return resolve_server(server_id, db=db)


def _migrate(path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)


def test_the_step_is_registered_and_current():
    assert SCHEMA_VERSION >= 110
    assert MIGRATIONS[107] is migrate_107_to_108
    assert MIGRATIONS[108] is migrate_mcp_secrets
    assert MIGRATIONS[109] is migrate_109_to_110


def test_v109_upgrade_marks_a_legacy_token_plan_endpoint_interactive_only(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('legacy','external','external','legacy','Alibaba Token Plan',443,'online','t','t','t',?)",
        (json.dumps({"base_url": "https://token-plan.example.com/v1"}),),
    )
    db.execute("PRAGMA user_version=108")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.from_version, result.to_version) == (108, 110)
    assert endpoint_agent_use("external@legacy/glm-5.3") == "interactive_only"


def test_v110_upgrade_restricts_legacy_endpoints_without_overwriting_explicit_allowed(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('legacy-v109','external','external','legacy-v109','Alibaba Token Plan',443,'online','t','t','t',?)",
        (json.dumps({"base_url": "https://token-plan.example.com/v1"}),),
    )
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('allowed-v109','external','external','allowed-v109','Alibaba Token Plan',443,'online','t','t','t',?)",
        (json.dumps({"agent_use_restriction": "allowed"}),),
    )
    db.execute("PRAGMA user_version=109")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert SCHEMA_VERSION == 110
    assert (result.migrated, result.from_version, result.to_version) == (True, 109, 110)
    assert endpoint_agent_use("external@legacy-v109/glm-5.3") == "interactive_only"
    assert endpoint_agent_use("external@allowed-v109/glm-5.3") == "allowed"
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated



def test_v109_upgrade_skips_metadata_that_is_not_an_object(isolated_db):
    from infra.database import get_db

    db = get_db()
    for sid, raw in (("null-meta", "null"), ("list-meta", "[1]"), ("text-meta", '"x"')):
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES (?,'external','external',?,'Alibaba Token Plan',443,'online','t','t','t',?)",
            (sid, sid, raw),
        )
    db.execute("PRAGMA user_version=109")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.to_version) == (True, 110)
    rows = dict(db.execute("SELECT id, metadata_json FROM network_servers").fetchall())
    assert rows == {"null-meta": "null", "list-meta": "[1]", "text-meta": '"x"'}


def test_v109_upgrade_leaves_existing_endpoint_allowed(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('existing','external','external','existing','Existing endpoint',443,'online','t','t','t',?)",
        (json.dumps({"base_url": "https://existing.example/v1"}),),
    )
    db.execute("PRAGMA user_version=108")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.from_version, result.to_version) == (108, 110)
    assert endpoint_agent_use("external@existing/model") == "allowed"


def test_live_keys_move_and_removed_ones_are_dropped(state, tmp_path, caplog):
    path = tmp_path / "keys.db"
    _seed(path)
    with closing(sqlite3.connect(path)) as db:
        key_store.set_managed_value("server-ns-live2", "store-key-0004", db=db)
    caplog.set_level(logging.INFO)

    _migrate(path)

    assert _resolve(path, "ns-live1") == "live-key-0001"
    assert _resolve(path, "ns-live2") == "store-key-0004"
    assert _resolve(path, "ns-blank") is None
    assert _resolve(path, "ns-gone") is None
    with closing(sqlite3.connect(path)) as db:
        assert _meta(db, "ns-live1") == {"base_url": "https://example.test/v1"}
        assert _meta(db, "ns-live2") == {}
        assert _meta(db, "ns-blank") == {}
        assert _meta(db, "ns-gone") == {}
        assert db.execute("SELECT metadata_json FROM network_servers WHERE id = 'ns-bad'").fetchone()[0] == "{not json"
        entries = {r[0] for r in db.execute("SELECT name FROM key_store_entries WHERE set_at IS NOT NULL")}
        assert {"server-ns-live1", "server-ns-live2"} <= entries
        assert "server-ns-gone" not in entries and "server-ns-blank" not in entries
        assert FIELD not in json.dumps([r[0] for r in db.execute("SELECT metadata_json FROM network_servers")])

    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "servers=" in message
    for value in ("live-key", "store-key", "gone-key"):
        assert value not in message


def test_a_second_start_is_a_no_op(state, tmp_path):
    path = tmp_path / "again.db"
    _seed(path)
    _migrate(path)
    values_before = (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes()

    with closing(sqlite3.connect(path)) as db:
        before = [tuple(r) for r in db.execute("SELECT id, metadata_json FROM network_servers ORDER BY id")]
        db.execute("PRAGMA user_version=107")
        db.commit()
    for backup in tmp_path.glob("*.bak"):
        backup.unlink()
    _migrate(path)

    with closing(sqlite3.connect(path)) as db:
        assert [tuple(r) for r in db.execute("SELECT id, metadata_json FROM network_servers ORDER BY id")] == before
    assert (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes() == values_before
    assert _resolve(path, "ns-live1") == "live-key-0001"


def test_a_failure_after_the_store_write_loses_nothing_on_the_next_start(state, tmp_path, monkeypatch):
    path = tmp_path / "crash.db"
    _seed(path)

    import infra.migration_107_to_108_server_keys as step

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure after the store write")

    monkeypatch.setattr(step.json, "dumps", boom)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(MigrationError):
            migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 107
        assert _meta(db, "ns-live1")[FIELD] == "live-key-0001"
        # The row rolled back with the step: the key is still only in the metadata.
        assert resolve_server("ns-live1", db=db) is None

    monkeypatch.undo()
    monkeypatch.setattr("infra.config.STATE_ROOT", state)
    for backup in tmp_path.glob("*.bak"):
        backup.unlink()
    _migrate(path)
    with closing(sqlite3.connect(path)) as db:
        assert FIELD not in _meta(db, "ns-live1")
        assert resolve_server("ns-live1", db=db) == "live-key-0001"
        live = {r[0] for r in db.execute("SELECT value_ref FROM key_store_entries WHERE value_ref IS NOT NULL")}
    # What the failed attempt wrote is unreferenced, and the retry's sweep removed it.
    assert set(json.loads((key_store.keys_dir() / key_store.VALUES_FILE).read_text())) == live


def test_a_database_without_the_servers_table_passes():
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_107_to_108(db)


def test_a_key_the_store_cannot_take_stays_put_and_one_warning_counts_it(state, tmp_path, caplog):
    path = tmp_path / "unstorable.db"
    unstorable = "has a space " + "zz" + "testsecret" * 4
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _server(db, "ns-spaced", {FIELD: unstorable})
        _server(db, "NS_Upper", {FIELD: "zz" + "testsecret" * 4})
        db.execute("PRAGMA user_version=107")
        db.commit()
    caplog.set_level(logging.INFO)

    _migrate(path)
    with closing(sqlite3.connect(path)) as db:
        assert _meta(db, "ns-spaced")[FIELD] == unstorable

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "key store" in r.getMessage()]
    assert len(warnings) == 1
    assert "left 2 server key(s) in place" in warnings[0]
    assert "unstorable value=1, unusable server id=1" in warnings[0]
    for fragment in ("testsecret", "ns-spaced", "NS_Upper"):
        assert fragment not in warnings[0]


def test_a_stripped_server_key_is_overwritten_in_the_database_file(state, tmp_path):
    path = tmp_path / "secure.db"
    planted = "zz" + "testsecret" * 4
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        for number in range(8):
            _server(db, f"ns-live{number}", {"label": "x" * 100, FIELD: planted})
        db.execute("PRAGMA user_version=107")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 0
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    assert planted.encode() not in path.read_bytes()
