"""Install count: the local weekly record, its closed field list, and its value rules."""
from __future__ import annotations

import ast
import json
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.usage_fields import FIELDS, USAGE_EVENT
from infra.database import initialize_schema
from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS
from infra.usage_summary import (
    SummaryError, iso_week, record_current_install_week, record_install_week, release_tag, validate_summary,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
NEXT_WEEK = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
MACHINE = {"os": "macos", "chip": "arm64"}


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    initialize_schema(connection)
    yield connection
    connection.close()


def _rows(db):
    return [(key, json_attrs) for key, json_attrs in db.execute(
        "SELECT dedupe_key, attributes FROM outcome_events WHERE event_name=? ORDER BY 1", (USAGE_EVENT,))]


def _counters(db):
    return db.execute("SELECT count(*) FROM outcome_event_counters WHERE event_name=?", (USAGE_EVENT,)).fetchone()[0]


def test_iso_week_and_release_tag():
    assert iso_week(NOW) == "2026-W44"
    assert iso_week(datetime(2027, 1, 1, tzinfo=timezone.utc)) == "2026-W53"
    assert release_tag("0.4.1") == "0.4.1"
    assert [release_tag(v) for v in ("0.4.1-12-gabc123", "", None, "dev", "v0.4.1")] == ["dev"] * 5


def test_recording_is_once_per_week_and_holds_only_the_closed_fields(db):
    assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is True
    assert record_install_week(db, now=NOW, version="0.4.2", fields=MACHINE) is False  # the week's first record wins
    ((key, attributes),) = _rows(db)
    assert key == f"{USAGE_EVENT}:2026-W44"
    assert json.loads(attributes) == {"week": "2026-W44", "release": "0.4.1", "os": "macos", "chip": "arm64"}
    row = db.execute("SELECT source,provenance FROM outcome_events WHERE event_name=?", (USAGE_EVENT,)).fetchone()
    assert tuple(row) == ("corsia", "instrumented")


def test_a_restart_in_a_recorded_week_is_not_counted_as_a_duplicate_delivery(db):
    record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE)
    for _ in range(3):  # app restarts later in the same week
        assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is False
    assert _counters(db) == 0
    assert len(_rows(db)) == 1


def test_two_writers_in_one_week_leave_one_row_and_no_duplicate_count(tmp_path):
    path = tmp_path / "usage.db"
    setup = sqlite3.connect(path)
    initialize_schema(setup)
    setup.close()
    start = threading.Barrier(2)
    results: list[bool] = []

    def writer() -> None:
        connection = sqlite3.connect(path, timeout=30)
        try:
            start.wait()
            results.append(record_install_week(connection, now=NOW, version="0.4.1", fields=MACHINE))
        finally:
            connection.close()

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = sqlite3.connect(path)
    try:
        assert sorted(results) == [False, True]
        assert len(_rows(check)) == 1 and _counters(check) == 0
    finally:
        check.close()


def test_an_install_left_open_across_a_week_records_both_weeks(db, monkeypatch):
    monkeypatch.setattr("infra.database.get_db", lambda: db)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.1")
    for moment in (NOW, NOW, NEXT_WEEK, NEXT_WEEK):  # each sweep tick after the first in a week is a no-op
        record_current_install_week(moment)
    assert [key for key, _ in _rows(db)] == [f"{USAGE_EVENT}:2026-W44", f"{USAGE_EVENT}:2026-W45"]
    assert _counters(db) == 0


def test_unlisted_machine_leaves_os_and_chip_out(db):
    record_install_week(db, now=NOW, version=None, fields={})
    ((_, attributes),) = _rows(db)
    assert json.loads(attributes) == {"week": "2026-W44", "release": "dev"}


@pytest.mark.parametrize("extra", [{"user": "x"}, {"host": "h"}, {"locale": "en"}, {"thread_id": "t"}, {"delivery_id": "x"}])
def test_a_record_with_any_other_field_fails(extra):
    with pytest.raises(SummaryError, match="outside the closed list"):
        validate_summary({"week": "2026-W44", "release": "dev", **extra})


# Values both rule sets must agree on: the validator and the outcome-event allowlist build from the
# patterns and enums in core.usage_fields, so neither can admit what the other refuses.
_VALUES = {
    "week": ["2026-W44", "2026-W01", "2026-W53", "2026-W00", "2026-W54", "2026-44", "2026-W4", "٢٠٢٦-W44", "2026-W44\n", "x"],
    "release": ["0.4.1", "12.0.103", "dev", "0.4", "0.4.1.2", "v0.4.1", "0.4.1-rc1", "latest", "٠.٤.١", "0.4.1\n", ""],
    "os": ["macos", "windows", "linux", "beos", "MacOS", ""],
    "chip": ["arm64", "x86_64", "m4", "aarch64", ""],
}


def _validator_accepts(key, value):
    record = {"week": "2026-W44", "release": "dev", key: value}
    try:
        validate_summary(record)
    except SummaryError:
        return False
    return True


@pytest.mark.parametrize("key", sorted(_VALUES))
def test_the_allowlist_and_the_validator_admit_the_same_values(key):
    spec = ATTRIBUTE_ALLOWLISTS[USAGE_EVENT][key]
    for value in _VALUES[key]:
        assert spec.accepts(value) == _validator_accepts(key, value), (key, value)
    assert any(spec.accepts(v) for v in _VALUES[key]) and not all(spec.accepts(v) for v in _VALUES[key])


def test_the_allowlist_takes_its_field_names_from_the_usage_field_list():
    assert set(ATTRIBUTE_ALLOWLISTS[USAGE_EVENT]) == set(FIELDS) == {"week", "release", "os", "chip"}


def test_recording_makes_no_network_call(db, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("usage collection touched the network")

    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is True
    assert len(_rows(db)) == 1


def test_usage_modules_import_no_network_or_model_client():
    banned = {"socket", "ssl", "http", "urllib", "urllib3", "httpx", "requests", "aiohttp", "websockets", "litellm", "openai", "anthropic"}
    backend = Path(__file__).resolve().parent.parent
    for relative in ("infra/usage_summary.py", "core/usage_fields.py"):
        tree = ast.parse((backend / relative).read_text())
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not names & banned, relative
