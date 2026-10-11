"""Schema v107 moves every cloud-provider, web-search and webhook key into the key store.

The claims: each source lands in the store with the documented precedence (store
value, then settings, then environment); the settings fields and the environment
stop being read; a second start changes nothing; a failure after the store
write but before the settings cleanup loses nothing on the next start; and a
store in the earlier flat format moves under references. Values resolve
through the migrated database's rows. Every value here is synthetic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing

import pytest

from infra import key_store, key_store_providers
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_106_to_107_key_store_credentials import migrate_106_to_107
from infra.schema import SCHEMA_VERSION

ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY")


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return root


def _put(db: sqlite3.Connection, key: str, value: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, 't')",
        (key, json.dumps(value)),
    )


def _setting(db: sqlite3.Connection, key: str) -> dict:
    return json.loads(db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0])


def _seed(path, monkeypatch) -> None:
    """A version-106 database holding a key in every source the step reads."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _put(db, "providers", {
            "anthropic": {"apiKey": "settings-anthropic-0001", "enabled": True},
            "openai": {"apiKey": "settings-openai-0002", "apiBase": "https://example.test"},
            "groq": {"apiKey": "settings-groq-0003"},
            "gemini": {"apiKey": ""},
            "omlx": {"apiKey": "lm-studio", "autoStart": True},
        })
        _put(db, "tools", {"web": {"search": {"apiKey": "settings-search-0004", "count": 3}}})
        _put(db, "webhook", {"github_webhook_secret": "settings-hook-0005", "other": 1})
        db.execute("PRAGMA user_version=106")
        db.commit()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-0006")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-0007")
    monkeypatch.setenv("BRAVE_API_KEY", "env-search-0008")


def _seed_flat(path, values: dict[str, str]) -> None:
    """A store as the earlier flat format left it: values under entry names, rows with no reference."""
    folder = key_store.keys_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / key_store.VALUES_FILE).write_text(json.dumps(values))
    with closing(sqlite3.connect(path)) as db:
        for name in values:
            db.execute(
                "INSERT INTO key_store_entries (name, created_at, set_at) VALUES (?, 't', 't')", (name,),
            )
        db.commit()


def _file_keys() -> set[str]:
    return set(json.loads((key_store.keys_dir() / key_store.VALUES_FILE).read_text()))


def _live_refs(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT value_ref FROM key_store_entries WHERE value_ref IS NOT NULL")}


def _clear_backups(directory) -> None:
    """The runner names a backup to the second and refuses to overwrite one."""
    for backup in directory.glob("*.bak"):
        backup.unlink()


def _forget_environment(monkeypatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_the_step_is_registered_and_current():
    assert SCHEMA_VERSION >= 107
    assert MIGRATIONS[106] is migrate_106_to_107


def test_every_source_lands_in_the_store_and_the_old_homes_are_cleared(state, tmp_path, monkeypatch, caplog):
    path = tmp_path / "keys.db"
    _seed(path, monkeypatch)
    # A store value set before the step wins over settings and environment.
    _seed_flat(path, {"provider-openai": "store-openai-0009"})
    caplog.set_level(logging.INFO)

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version) == (True, 106)

        assert key_store.resolve_provider("anthropic", db=db) == "settings-anthropic-0001"
        assert key_store.resolve_provider("openai", db=db) == "store-openai-0009"
        assert key_store.resolve_provider("groq", db=db) == "settings-groq-0003"
        assert key_store.resolve_provider("gemini", db=db) is None
        assert key_store.resolve_provider("omlx", db=db) == "lm-studio"
        assert key_store.resolve_builtin(key_store.WEB_SEARCH, db=db) == ("settings-search-0004", key_store.SOURCE_STORE)
        assert key_store.resolve_builtin(key_store.GITHUB_WEBHOOK, db=db) == ("settings-hook-0005", key_store.SOURCE_STORE)
        assert _file_keys() == _live_refs(db)

        providers = _setting(db, "providers")
        assert all("apiKey" not in cfg for cfg in providers.values())
        assert providers["anthropic"] == {"enabled": True}
        assert providers["omlx"] == {"autoStart": True}
        assert _setting(db, "tools") == {"web": {"search": {"count": 3}}}
        assert _setting(db, "webhook") == {"other": 1}

        rows = {r["name"] for r in db.execute("SELECT name FROM key_store_entries WHERE set_at IS NOT NULL")}
        assert {"provider-anthropic", "provider-openai", "provider-groq", "web-search", "github-webhook"} <= rows

    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "providers=" in message
    for value in ("settings-", "env-", "store-openai"):
        assert value not in message


def test_environment_fills_only_what_settings_and_store_lack(state, tmp_path, monkeypatch):
    path = tmp_path / "env.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("PRAGMA user_version=106")
        db.commit()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-0006")
    monkeypatch.setenv("BRAVE_API_KEY", "env-search-0008")

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        assert key_store.resolve_provider("anthropic", db=db) == "env-anthropic-0006"
        assert key_store.resolve_provider("openai", db=db) is None
        assert key_store.resolve_builtin(key_store.WEB_SEARCH, db=db) == ("env-search-0008", key_store.SOURCE_STORE)


def test_after_the_step_neither_settings_nor_environment_is_read(state, tmp_path, monkeypatch):
    path = tmp_path / "after.db"
    _seed(path, monkeypatch)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        _put(db, "providers", {"anthropic": {"apiKey": "late-settings-0010"}})
        db.commit()
    monkeypatch.setenv("OPENAI_API_KEY", "late-env-0011")
    monkeypatch.setenv("BRAVE_API_KEY", "late-env-0012")
    from infra.key_store_providers import provider_keys

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        key_store.drop_managed_entry("provider-openai", db=db)
        key_store.clear_value("web-search", db=db)

        assert key_store.resolve_provider("anthropic", db=db) == "settings-anthropic-0001"
        assert key_store.resolve_provider("openai", db=db) is None
        assert key_store.resolve_builtin(key_store.WEB_SEARCH, db=db) is None
        assert "openai" not in provider_keys(db=db)


def test_a_second_start_is_a_no_op(state, tmp_path, monkeypatch):
    path = tmp_path / "again.db"
    _seed(path, monkeypatch)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
    values_before = (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes()

    # Running the step itself again, even on a database that is back at 106, changes nothing.
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        settings_before = [tuple(r) for r in db.execute("SELECT key, value FROM settings ORDER BY key")]
        db.execute("PRAGMA user_version=106")
        db.commit()
        _clear_backups(tmp_path)
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        assert [tuple(r) for r in db.execute("SELECT key, value FROM settings ORDER BY key")] == settings_before
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    assert (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes() == values_before


def test_a_failure_after_the_store_write_loses_nothing_on_the_next_start(state, tmp_path, monkeypatch):
    path = tmp_path / "crash.db"
    _seed(path, monkeypatch)

    import infra.migration_106_to_107_key_store_credentials as step

    real = step._write_setting

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure before the settings cleanup")

    monkeypatch.setattr(step, "_write_setting", boom)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(MigrationError):
            migrate_to_current(db, SCHEMA_VERSION)
        # Rolled back: the old copies are all still there and the version did not move.
        assert db.execute("PRAGMA user_version").fetchone()[0] == 106
        assert _setting(db, "providers")["anthropic"]["apiKey"] == "settings-anthropic-0001"
        # The rows rolled back with it, so the values written before the failure are
        # unreferenced: nothing resolves to them, and the next commit's sweep removes them.
        assert key_store.resolve_provider("anthropic", db=db) is None
        assert _file_keys() and not _live_refs(db)

    monkeypatch.setattr(step, "_write_setting", real)
    _forget_environment(monkeypatch)  # the retry must not need the environment either
    _clear_backups(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        assert "apiKey" not in _setting(db, "providers")["anthropic"]
        assert key_store.resolve_provider("anthropic", db=db) == "settings-anthropic-0001"
        assert key_store.resolve_provider("groq", db=db) == "settings-groq-0003"
        assert key_store.resolve_builtin(key_store.GITHUB_WEBHOOK, db=db) == ("settings-hook-0005", key_store.SOURCE_STORE)
        assert key_store.resolve_builtin(key_store.WEB_SEARCH, db=db) == ("settings-search-0004", key_store.SOURCE_STORE)
        assert key_store_providers.provider_keys(db=db)["openai"] == "settings-openai-0002"
        assert _file_keys() == _live_refs(db)


def test_a_value_that_cannot_be_stored_stays_where_it_was(state, tmp_path):
    path = tmp_path / "odd.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _put(db, "providers", {"openai": {"apiKey": "has a space"}, "anthropic": {"apiKey": "ok-value-0013"}})
        db.execute("PRAGMA user_version=106")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        providers = _setting(db, "providers")
    assert providers["openai"] == {"apiKey": "has a space"}
    assert "apiKey" not in providers["anthropic"]
    with closing(sqlite3.connect(path)) as db:
        assert key_store.resolve_provider("openai", db=db) is None
        assert key_store.resolve_provider("anthropic", db=db) == "ok-value-0013"


def test_a_value_the_store_cannot_take_stays_put_and_one_warning_counts_it(state, tmp_path, monkeypatch, caplog):
    path = tmp_path / "unstorable.db"
    field = "api" + "Key"
    hook = "github_webhook_" + "sec" + "ret"
    env_name = "BRAVE_API" + "_KEY"
    unstorable = "has a space " + "zz" + "testsecret" * 4
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _put(db, "providers", dict(anthropic={field: unstorable}, openai={field: "x" * 5000}))
        _put(db, "tools", dict(web=dict(search={field: unstorable})))
        _put(db, "webhook", {hook: unstorable})
        db.execute("PRAGMA user_version=106")
        db.commit()
    monkeypatch.setenv(env_name, unstorable)
    caplog.set_level(logging.INFO)

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        assert _setting(db, "providers")["anthropic"][field] == unstorable
        assert _setting(db, "webhook")[hook] == unstorable

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "key store" in r.getMessage()]
    assert len(warnings) == 1
    assert "left 5 value(s) in place" in warnings[0]
    assert "environment=1 providers=2 web-search=1 webhook=1" in warnings[0]
    for fragment in ("testsecret", "xxxxx", "anthropic", env_name):
        assert fragment not in warnings[0]


def test_stripped_settings_keys_are_overwritten_in_the_database_file(state, tmp_path):
    path = tmp_path / "secure.db"
    field = "api" + "Key"
    hook = "github_webhook_" + "sec" + "ret"
    planted = "zz" + "testsecret" * 4
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _put(db, "providers", dict(anthropic={field: planted, "enabled": True}))
        _put(db, "tools", dict(web=dict(search={field: planted + "b", "count": 3})))
        _put(db, "webhook", {hook: planted + "c", "other": 1})
        db.execute("PRAGMA user_version=106")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 0  # the setting is restored
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    assert planted.encode() not in path.read_bytes()


def test_a_flat_format_store_moves_under_references_and_the_flat_names_go(state, tmp_path):
    path = tmp_path / "flat.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("PRAGMA user_version=106")
        db.commit()
    _seed_flat(path, {"provider-openai": "flat-openai-0014", "web-search": "flat-search-0015"})

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        refs = dict(db.execute("SELECT name, value_ref FROM key_store_entries"))
        assert key_store.resolve_provider("openai", db=db) == "flat-openai-0014"
        assert key_store.resolve_builtin(key_store.WEB_SEARCH, db=db) == ("flat-search-0015", key_store.SOURCE_STORE)
    assert refs["provider-openai"].startswith("provider-openai.")
    assert refs["web-search"].startswith("web-search.")
    # The flat names are unreferenced once the step commits, and its sweep removes them.
    assert _file_keys() == {refs["provider-openai"], refs["web-search"]}
