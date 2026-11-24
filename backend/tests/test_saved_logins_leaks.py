"""A saved login's password goes nowhere but the values file, and only named code reads it.

A sentinel password is saved, read for a fill, marked out of date and
deleted, and every refusal path is driven with it. It must not appear in the
database file, the logs, any event or websocket message, any exception or any
view; it lives only in the values file, and not even there once deleted.
An import allowlist pins which modules may name the readers.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
import uuid
from pathlib import Path

import pytest

from infra import key_store, saved_logins
from infra.database import get_db
from infra.key_store_values import KeyStoreError

BACKEND = Path(__file__).resolve().parents[1]
#: The only modules that may name a password reader. A module that fills a page
#: or blanks browser output joins this list on purpose, in its own reviewed change.
READER_ALLOWLIST = frozenset({
    "infra/saved_logins.py",
    "tests/test_saved_logins.py",
    "tests/test_saved_logins_leaks.py",
    "tests/test_saved_logins_keys_routes.py",
})
READERS = ("password_for_fill", "blanking_values")


@pytest.fixture
def state(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    return root


@pytest.fixture
def emitted(monkeypatch) -> list[str]:
    """Everything handed to the websocket senders and the event emitters, as text."""
    from infra import outcome_events, product_events
    from infra.state import ws

    seen: list[str] = []

    def spy(name):
        def record(*args, **kwargs):
            seen.append(f"{name} {args!r} {kwargs!r}")
        return record

    for module, names in (
        (ws, ("_schedule_send", "send_to_client", "broadcast_to_thread", "send_to_thread_sessions",
              "broadcast_event", "broadcast_to_user", "broadcast_all")),
        (product_events, ("emit_product_event",)),
        (outcome_events, ("record_outcome_event",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, spy(name))
    return seen


def _sentinel() -> tuple[str, str]:
    """A password with a space and Unicode, and the unique ASCII marker inside it."""
    marker = uuid.uuid4().hex
    return f"Zq9 sentinel ü-{marker}", marker


def _forms(password: str, marker: str) -> list[bytes]:
    """Every way the password could be written down: raw, JSON-escaped, UTF-16, and its unique part."""
    return [
        password.encode("utf-8"),
        json.dumps(password).encode("ascii"),
        password.encode("utf-16-le"),
        marker.encode("ascii"),
        marker.encode("utf-16-le"),
    ]


def _refusals(password: str, login_id: str) -> list[BaseException]:
    """Drive every refusal path with the password in the arguments; return what each raised."""
    attempts = [
        lambda: saved_logins.save_login(site="http://example.com", username="u", password=password, saved_by="p"),
        lambda: saved_logins.save_login(site=password, username="u", password=password, saved_by="p"),
        lambda: saved_logins.save_login(site="https://example.com", username="u\x00", password=password, saved_by="p"),
        lambda: saved_logins.save_login(site="https://example.com", username=password + "\x00", password="x", saved_by="p"),
        lambda: saved_logins.save_login(site="https://example.com", username="u", password=password + "\x00", saved_by="p"),
        lambda: saved_logins.save_login(site="https://example.com", username="u", password=password * 200, saved_by="p"),
        lambda: saved_logins.save_login(site="https://example.com", username="u", password=password, saved_by=""),
        lambda: saved_logins.get_login(password),
        lambda: saved_logins.password_for_fill(password),
        lambda: saved_logins.mark_out_of_date(password),
        lambda: saved_logins.record_use(login_id, ""),
        lambda: saved_logins.record_use(password, "thread"),
        lambda: saved_logins.delete_login(password),
        lambda: saved_logins.logins_for_site(password),
    ]
    raised = []
    for attempt in attempts:
        with pytest.raises(KeyStoreError) as refused:
            attempt()
        raised.append(refused.value)
    return raised


def _exception_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(error)) + repr(error) + repr(error.args)


def _files_except_values(root: Path) -> dict[Path, bytes]:
    get_db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != key_store.VALUES_FILE
    }


def test_the_password_appears_nowhere_but_the_values_file(state, tmp_path, caplog, emitted):
    password, marker = _sentinel()
    forms = _forms(password, marker)
    snapshots: list[dict[Path, bytes]] = []
    with caplog.at_level(logging.DEBUG):
        login_id = saved_logins.save_login(
            site="https://example.com", username="me@example.com", password=password, saved_by="person",
        )
        snapshots.append(_files_except_values(tmp_path))
        assert saved_logins.password_for_fill(login_id) == password
        assert password in saved_logins.blanking_values()
        saved_logins.record_use(login_id, "thread-1")
        saved_logins.mark_out_of_date(login_id)
        snapshots.append(_files_except_values(tmp_path))
        views = [saved_logins.get_login(login_id), *saved_logins.logins_for_site("https://example.com")]
        errors = _refusals(password, login_id)
        values_file = (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes()
        saved_logins.delete_login(login_id)
        snapshots.append(_files_except_values(tmp_path))

    assert json.dumps(password).encode("ascii") in values_file
    assert password in json.loads(values_file).values()
    assert all(any(path.name.endswith(".db") for path in snapshot) for snapshot in snapshots)
    for snapshot in snapshots:
        for path, data in snapshot.items():
            assert not any(form in data for form in forms), path
    logged = caplog.text + "".join(repr(record.args) + repr(record.msg) for record in caplog.records)
    for text in (logged, "".join(emitted), repr(views), *(_exception_text(error) for error in errors)):
        assert password not in text
        assert marker not in text


def test_deleting_a_login_takes_its_password_out_of_the_values_file(state, tmp_path):
    password, marker = _sentinel()
    login_id = saved_logins.save_login(
        site="https://example.com", username="me", password=password, saved_by="person",
    )
    saved_logins.delete_login(login_id)
    from key_store_fixtures import values_after_a_sweep

    assert password not in values_after_a_sweep().values()
    for path, data in _files_except_values(tmp_path).items():
        assert marker.encode() not in data, path
    values_path = key_store.keys_dir() / key_store.VALUES_FILE
    assert marker.encode() not in values_path.read_bytes()


def test_a_replaced_password_leaves_the_values_file(state):
    old, marker = _sentinel()
    saved_logins.save_login(site="https://example.com", username="me", password=old, saved_by="person")
    saved_logins.save_login(site="https://example.com", username="me", password="the replacement", saved_by="person")
    assert marker.encode() not in (key_store.keys_dir() / key_store.VALUES_FILE).read_bytes()


def test_no_key_store_reader_returns_a_login_password(state):
    password, _marker = _sentinel()
    login_id = saved_logins.save_login(
        site="https://example.com", username="me", password=password, saved_by="person",
    )
    name = saved_logins.entry_name(login_id)
    assert key_store.resolve_names([name]) == {}
    assert name not in {entry["name"] for entry in key_store.list_entries()}
    with pytest.raises(KeyStoreError):
        key_store.entry_metadata(name)


def _modules_naming_a_reader() -> set[str]:
    pattern = re.compile(r"\b(" + "|".join(READERS) + r")\b")
    return {
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if ".venv" not in path.parts and pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    }


def test_only_the_allowlisted_modules_name_a_password_reader():
    assert _modules_naming_a_reader() <= READER_ALLOWLIST, (
        "A module outside the allowlist names a saved-login password reader. A fill or blanking module "
        "joins READER_ALLOWLIST on purpose, in its own reviewed change."
    )
    assert "infra/saved_logins.py" in _modules_naming_a_reader()
