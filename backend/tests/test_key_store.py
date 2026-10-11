"""The key store's storage and rules: where values live, and what never carries one.

Values live in one 0600 file in a 0700 folder of CORSIA's private state.
Metadata lives in the database and never holds a value; nothing that reads,
backs up or exports the database or the settings table can carry one. The
database commit is the only commit point: a rolled-back write leaves the old
value, and a sweep removes every value no committed row references.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading

import pytest

from infra import key_store, key_store_values
from infra.database import get_db, write_transaction
from infra.db_connection import _NoOpClose
from infra.settings import set_setting

VALUE = "BSAkeystore-test-value-0123456789"
LEGACY_TOOLS = {"web": {"search": {"apiKey": "legacy-settings-value", "count": 3}}}


@pytest.fixture
def state(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
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

    monkeypatch.setattr(key_store_values.os, "open", spy)
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

    monkeypatch.setattr(key_store_values.os, "replace", broken)
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


def test_search_reads_the_store_and_ignores_the_environment(state, monkeypatch):
    builtin = key_store.WEB_SEARCH
    assert key_store.resolve_builtin(builtin) is None
    monkeypatch.setenv("BRAVE_API_KEY", "env-value")
    assert key_store.resolve_builtin(builtin) is None
    assert key_store.entry_metadata("web-search")["is_set"] is False
    key_store.set_value("web-search", VALUE)
    assert key_store.resolve_builtin(builtin) == (VALUE, key_store.SOURCE_STORE)
    entry = key_store.entry_metadata("web-search")
    assert entry["is_set"] is True


@pytest.mark.parametrize("tools", [None, {}, {"web": "off"}, {"web": {"search": {"count": 3}}}, LEGACY_TOOLS])
def test_the_settings_table_is_neither_read_nor_written_for_a_built_in(state, tools):
    from infra.settings import get_setting

    if tools is not None:
        set_setting("tools", tools)
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) is None
    assert key_store.entry_metadata("web-search")["is_set"] is False
    key_store.set_value("web-search", VALUE)
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) == (VALUE, key_store.SOURCE_STORE)
    assert get_setting("tools") == tools
    key_store.clear_value("web-search")
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) is None
    assert get_setting("tools") == tools


def test_resolving_never_writes_the_value_into_the_environment(state):
    key_store.set_value("web-search", VALUE)
    key_store.resolve_builtin(key_store.WEB_SEARCH)
    assert VALUE not in os.environ.values()


def _file_refs() -> set[str]:
    return set(json.loads((key_store.keys_dir() / key_store.VALUES_FILE).read_text()))


def _live_refs() -> set[str]:
    return {r[0] for r in get_db().execute("SELECT value_ref FROM key_store_entries WHERE value_ref IS NOT NULL")}


def _refuse_commit(_self):
    raise RuntimeError("commit failed")


@pytest.mark.parametrize("failure", ["raise", "commit"])
def test_a_rolled_back_write_keeps_the_committed_value_and_a_sweep_removes_its_own(state, monkeypatch, failure):
    key_store.set_managed_value("provider-openai", "kept-old")

    with monkeypatch.context() as patch, pytest.raises(RuntimeError):
        if failure == "commit":
            patch.setattr(_NoOpClose, "commit", _refuse_commit, raising=False)
        with write_transaction(None, scope="test_key_rollback") as db:
            key_store.set_managed_value("provider-openai", "replaced", db=db)
            key_store.set_managed_value("provider-groq", "added", db=db)
            if failure == "raise":
                raise RuntimeError("host write failed")

    assert key_store.resolve_names(["provider-openai", "provider-groq"]) == {"provider-openai": "kept-old"}
    assert len(_file_refs()) == 3  # the rolled-back values are unreferenced, not live
    assert key_store_values.sweep() == 2
    assert _file_refs() == _live_refs()
    assert key_store.resolve_names(["provider-openai"]) == {"provider-openai": "kept-old"}


def test_the_startup_sweep_removes_what_a_crash_left_unreferenced(state, monkeypatch, caplog):
    # The process dies before any after-commit sweep runs, and once mid-transaction.
    with monkeypatch.context() as patch:
        patch.setattr(key_store_values, "sweep_after_commit", lambda _conn: None)
        key_store.set_value("web-search", VALUE)
        key_store.set_value("web-search", VALUE + "-new")
        with pytest.raises(RuntimeError):
            with write_transaction(None, scope="test_key_crash") as db:
                key_store.set_managed_value("provider-openai", "never-committed", db=db)
                raise RuntimeError("crash")
    assert len(_file_refs()) == 3 and len(_live_refs()) == 1

    caplog.set_level(logging.INFO)
    key_store_values.sweep_at_startup()

    assert _file_refs() == _live_refs()
    assert key_store.resolve_builtin(key_store.WEB_SEARCH) == (VALUE + "-new", key_store.SOURCE_STORE)
    assert any("removed 2 unreferenced value(s)" in r.getMessage() for r in caplog.records)
    assert VALUE not in caplog.text


def test_a_sweep_waits_for_an_open_write_and_keeps_its_value(state):
    written, release = threading.Event(), threading.Event()
    refs: list[str] = []
    swept: list[int] = []

    def writer():
        with write_transaction(None, scope="test_key_in_flight") as db:
            refs.append(key_store_values.put(db, "provider-openai", "in-flight-0001"))
            db.execute(
                "INSERT INTO key_store_entries (name, created_at, set_at, value_ref) VALUES (?, 't', 't', ?)",
                ("provider-openai", refs[0]),
            )
            written.set()
            release.wait(10)

    writing = threading.Thread(target=writer)
    writing.start()
    try:
        assert written.wait(10)
        sweeping = threading.Thread(target=lambda: swept.append(key_store_values.sweep()))
        sweeping.start()
        sweeping.join(0.5)
        assert sweeping.is_alive()  # it waits for the open write's lock
        assert refs[0] in _file_refs()
    finally:
        release.set()
        writing.join(10)
    sweeping.join(10)

    assert swept == [0]
    assert key_store.resolve_provider("openai") == "in-flight-0001"
    assert _file_refs() == {refs[0]}
