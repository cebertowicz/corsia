"""The key store's storage and rules: where values live, and what never carries one.

Values live in one 0600 file in a 0700 folder of CORSIA's private state.
Metadata lives in the database and never holds a value; nothing that reads,
backs up or exports the database or the settings table can carry one.
"""

from __future__ import annotations

import os
import stat

import pytest

from infra import key_store
from infra.database import get_db
from infra.settings import set_setting

VALUE = "BSAkeystore-test-value-0123456789"


@pytest.fixture
def state(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.delenv(key_store.WEB_SEARCH.env_var, raising=False)
    return root


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _database_bytes(db_path) -> bytes:
    get_db().execute("PRAGMA wal_checkpoint(FULL)")
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = f"{db_path}{suffix}"
        if os.path.exists(candidate):
            with open(candidate, "rb") as handle:
                blob += handle.read()
    return blob


def test_values_file_is_0600_in_a_0700_folder_even_with_an_open_umask(state):
    previous = os.umask(0)
    try:
        key_store.set_value("web-search", VALUE)
    finally:
        os.umask(previous)
    folder = key_store.keys_dir()
    assert _mode(folder) == 0o700
    assert _mode(folder / key_store.VALUES_FILE) == 0o600


def test_every_write_creates_its_temporary_file_0600_in_the_open_call(state, monkeypatch):
    opened: list[tuple[str, int, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(key_store.os, "open", spy)
    key_store.set_value("web-search", VALUE)
    temporaries = [entry for entry in opened if entry[0].endswith(".tmp")]
    assert len(temporaries) == 1
    _, flags, mode = temporaries[0]
    assert mode == 0o600
    assert flags & os.O_CREAT and flags & os.O_EXCL
    assert not list(key_store.keys_dir().glob("*.tmp"))


def test_a_failed_write_leaves_the_file_unchanged_and_no_temporary(state, monkeypatch):
    key_store.set_value("web-search", VALUE)
    path = key_store.keys_dir() / key_store.VALUES_FILE
    before = path.read_bytes()

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(key_store.os, "replace", broken)
    with pytest.raises(OSError):
        key_store.set_value("web-search", "BSAanother-value-9876543210")
    assert path.read_bytes() == before
    assert not list(key_store.keys_dir().glob("*.tmp"))


def test_values_never_reach_the_database_or_the_settings_table(state, isolated_db):
    key_store.set_value("web-search", VALUE)
    key_store.add_entry("maps", "Maps", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value("maps", VALUE + "-maps")
    assert VALUE.encode() not in _database_bytes(isolated_db)
    rows = get_db().execute("SELECT value FROM settings").fetchall()
    assert all(VALUE not in row["value"] for row in rows)


def test_metadata_carries_labels_and_dates_and_never_a_value(state):
    key_store.set_value("web-search", VALUE)
    entries = key_store.list_entries()
    assert VALUE not in repr(entries)
    web = entries[0]
    assert web["name"] == "web-search"
    assert web["label"] == "Web search"
    assert web["used_by"] == "web search"
    assert web["is_set"] is True
    assert web["source"] == key_store.SOURCE_STORE
    assert web["set_at"]


def test_add_refuses_a_name_in_use_including_the_built_in(state):
    key_store.add_entry("maps", "Maps", "Map tiles", added_by_thread_id="t-1", added_by_name="conductor")
    with pytest.raises(key_store.KeyStoreError, match="already exists"):
        key_store.add_entry("maps", "Maps again", "", added_by_thread_id=None, added_by_name=None)
    with pytest.raises(key_store.KeyStoreError, match="already exists"):
        key_store.add_entry("web-search", "Search", "", added_by_thread_id=None, added_by_name=None)


@pytest.mark.parametrize("name,label,description", [
    ("Maps", "Maps", ""),
    ("maps_v2", "Maps", ""),
    ("m" * 41, "Maps", ""),
    ("", "Maps", ""),
    ("maps", "", ""),
    ("maps", "x" * 81, ""),
    ("maps", "Maps", "x" * 281),
    ("maps", 7, ""),
])
def test_add_validates_name_label_and_description(state, name, label, description):
    with pytest.raises(key_store.KeyStoreError):
        key_store.add_entry(name, label, description, added_by_thread_id=None, added_by_name=None)


def test_an_added_entry_shows_who_added_it_and_no_use(state):
    key_store.add_entry("maps", "Maps", "Map tiles", added_by_thread_id="t-1", added_by_name="Conductor")
    entry = key_store.entry_metadata("maps")
    assert entry["added_by"] == "Conductor"
    assert entry["used_by"] is None
    assert entry["is_set"] is False
    assert entry["checkable"] is False


def test_clear_and_remove(state):
    key_store.add_entry("maps", "Maps", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value("maps", VALUE)
    key_store.clear_value("maps")
    assert key_store.entry_metadata("maps")["is_set"] is False
    key_store.set_value("maps", VALUE)
    key_store.remove_entry("maps")
    with pytest.raises(key_store.KeyStoreError):
        key_store.entry_metadata("maps")
    assert "maps" not in (key_store.keys_dir() / key_store.VALUES_FILE).read_text()
    with pytest.raises(key_store.KeyStoreError, match="cannot be removed|can be cleared"):
        key_store.remove_entry("web-search")


def test_set_refuses_an_unknown_entry_and_a_value_that_cannot_be_a_header(state):
    with pytest.raises(key_store.KeyStoreError):
        key_store.set_value("nope", VALUE)
    for bad in ("", "   ", "two words", "line\nbreak", "x" * (key_store.VALUE_MAX + 1), None):
        with pytest.raises(key_store.KeyStoreError) as refused:
            key_store.set_value("web-search", bad)
        if isinstance(bad, str) and bad.strip():
            assert bad not in str(refused.value)


def test_search_reads_the_store_then_the_settings_value_then_the_environment(state, monkeypatch):
    builtin = key_store.WEB_SEARCH
    assert key_store.resolve_builtin(builtin) is None
    monkeypatch.setenv(builtin.env_var, "env-value")
    assert key_store.resolve_builtin(builtin) == ("env-value", key_store.SOURCE_ENVIRONMENT)
    set_setting("tools", {"web": {"search": {"apiKey": "settings-value"}}})
    assert key_store.resolve_builtin(builtin) == ("settings-value", key_store.SOURCE_SETTINGS)
    assert key_store.entry_metadata("web-search")["source"] == key_store.SOURCE_SETTINGS
    key_store.set_value("web-search", VALUE)
    assert key_store.resolve_builtin(builtin) == (VALUE, key_store.SOURCE_STORE)
    entry = key_store.entry_metadata("web-search")
    assert entry["source"] == key_store.SOURCE_STORE


def test_setting_the_built_in_deletes_its_older_settings_copy_and_nothing_else(state):
    from infra.settings import get_setting

    legacy = "legacy-settings-value"
    set_setting("tools", {"web": {"search": {"apiKey": legacy, "count": 3}}, "shell": {"on": True}})
    key_store.set_value("web-search", VALUE)
    assert get_setting("tools") == {"web": {"search": {"count": 3}}, "shell": {"on": True}}
    rows = get_db().execute("SELECT value FROM settings").fetchall()
    assert all(legacy not in row["value"] and VALUE not in row["value"] for row in rows)
    key_store.clear_value("web-search")
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) is None


@pytest.mark.parametrize("tools", [None, {}, {"web": "off"}, {"web": {"search": {"count": 3}}}])
def test_setting_the_built_in_succeeds_with_no_older_settings_copy(state, tools):
    from infra.settings import get_setting

    if tools is not None:
        set_setting("tools", tools)
    key_store.set_value("web-search", VALUE)
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) == (VALUE, key_store.SOURCE_STORE)
    assert get_setting("tools") == tools


def test_setting_an_added_entry_leaves_the_settings_copy_alone(state):
    from infra.settings import get_setting

    tools = {"web": {"search": {"apiKey": "legacy-settings-value"}}}
    set_setting("tools", tools)
    key_store.add_entry("maps", "Maps", "", added_by_thread_id=None, added_by_name=None)
    key_store.set_value("maps", VALUE)
    assert get_setting("tools") == tools


def test_resolving_never_writes_the_value_into_the_environment(state):
    key_store.set_value("web-search", VALUE)
    key_store.resolve_builtin(key_store.WEB_SEARCH)
    assert VALUE not in os.environ.values()


def test_startup_no_longer_copies_the_settings_value_into_the_environment(state, monkeypatch):
    from infra.settings import bootstrap_env

    set_setting("tools", {"web": {"search": {"apiKey": "settings-value"}}})
    bootstrap_env()
    assert "settings-value" not in os.environ.values()
    assert key_store.WEB_SEARCH.env_var not in os.environ
