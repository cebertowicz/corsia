"""Install count: the local weekly record, its closed field list, and its value rules."""
from __future__ import annotations

import ast
import asyncio
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
from infra.usage_summary import iso_week, platform_fields, record_current_install_week, record_install_week, release_tag

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
NEXT_WEEK = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
MACHINE = {"os.type": "darwin", "host.arch": "arm64"}


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    initialize_schema(connection)
    yield connection
    connection.close()


def _rows(db):
    return [(key, attributes) for key, attributes in db.execute(
        "SELECT dedupe_key, attributes FROM outcome_events WHERE event_name=? ORDER BY 1", (USAGE_EVENT,))]


def _counters(db):
    return db.execute("SELECT count(*) FROM outcome_event_counters WHERE event_name=?", (USAGE_EVENT,)).fetchone()[0]


def test_iso_week_and_release_tag():
    assert iso_week(NOW) == "2026-W44"
    assert iso_week(datetime(2027, 1, 1, tzinfo=timezone.utc)) == "2026-W53"
    assert release_tag("0.4.1") == "0.4.1"
    assert [release_tag(v) for v in ("0.4.1-12-gabc123", "", None, "dev", "v0.4.1")] == ["dev"] * 5


@pytest.mark.parametrize("system,machine,expected", [
    ("Darwin", "arm64", {"os.type": "darwin", "host.arch": "arm64"}),
    ("Linux", "aarch64", {"os.type": "linux", "host.arch": "arm64"}),
    ("Windows", "AMD64", {"os.type": "windows", "host.arch": "amd64"}),
    ("Linux", "x86_64", {"os.type": "linux", "host.arch": "amd64"}),
    ("Other", "other", {}),
])
def test_platform_fields(system, machine, expected, monkeypatch):
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert platform_fields() == expected


def test_recording_is_once_per_week_and_holds_only_the_closed_fields(db):
    assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is True
    assert record_install_week(db, now=NOW, version="0.4.2", fields=MACHINE) is False  # the week's first record wins
    ((key, attributes),) = _rows(db)
    assert key == f"{USAGE_EVENT}:2026-W44"
    assert json.loads(attributes) == {"corsia.usage.week": "2026-W44", "service.version": "0.4.1", "os.type": "darwin", "host.arch": "arm64"}
    row = db.execute("SELECT source,provenance FROM outcome_events WHERE event_name=?", (USAGE_EVENT,)).fetchone()
    assert tuple(row) == ("corsia", "instrumented")


def test_a_restart_in_a_recorded_week_is_not_counted_as_a_duplicate_delivery(db):
    record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE)
    for _ in range(3):  # app restarts later in the same week
        assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is False
    assert _counters(db) == 0
    assert len(_rows(db)) == 1


def test_a_recorded_week_takes_no_write_transaction(db, monkeypatch):
    import infra.usage_summary as usage

    opened: list[str] = []
    real = usage.write_transaction

    def spy(*args, **kwargs) -> object:
        opened.append(kwargs.get("scope", ""))
        return real(*args, **kwargs)

    monkeypatch.setattr(usage, "write_transaction", spy)
    assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is True
    for _ in range(3):
        assert record_install_week(db, now=NOW, version="0.4.1", fields=MACHINE) is False
    assert opened == ["record_install_week"]  # only the week's first record took the write lock


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


def test_the_presence_sweep_loop_survives_a_raising_recorder(monkeypatch):
    import ws.presence_control as control

    recorder_calls = 0

    async def fake_run_in_db(fn, *args, **kwargs):
        nonlocal recorder_calls
        if fn is control.usage_summary.record_current_install_week:
            recorder_calls += 1
            if recorder_calls == 3:
                raise asyncio.CancelledError  # ends the loop; an Exception would be swallowed by it
            raise RuntimeError("the recorder failed")

    async def fake_apply(fn, **kwargs):
        return None

    monkeypatch.setattr(control, "SWEEP_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(control, "run_in_db", fake_run_in_db)
    monkeypatch.setattr(control, "_apply", fake_apply)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(control.presence_sweep_loop())
    assert recorder_calls == 3  # two raising ticks did not end the loop


def test_unlisted_machine_leaves_os_and_chip_out(db):
    record_install_week(db, now=NOW, version=None, fields={})
    ((_, attributes),) = _rows(db)
    assert json.loads(attributes) == {"corsia.usage.week": "2026-W44", "service.version": "dev"}


@pytest.mark.parametrize("extra", [{"user": "x"}, {"host": "h"}, {"locale": "en"}, {"thread_id": "t"}, {"delivery_id": "x"}])
def test_a_field_outside_the_closed_list_is_dropped_and_counted(db, extra):
    record_install_week(db, now=NOW, version="0.4.1", fields={**MACHINE, **extra})
    ((_, attributes),) = _rows(db)
    assert json.loads(attributes) == {"corsia.usage.week": "2026-W44", "service.version": "0.4.1", **MACHINE}
    dropped = db.execute(
        "SELECT n FROM outcome_event_counters WHERE counter='attribute_dropped' AND event_name=?", (USAGE_EVENT,),
    ).fetchone()
    assert dropped[0] == 1


# The allowlist is the one enforcer of the value rules in core.usage_fields: each field takes exactly
# these values and refuses these others, including digits of other scripts and a trailing newline.
_VALUES = {
    "corsia.usage.week": (["2026-W44", "2026-W01", "2026-W53"], ["2026-W00", "2026-W54", "2026-44", "2026-W4", "٢٠٢٦-W44", "2026-W44\n", "x", 7]),
    "service.version": (["0.4.1", "12.0.103", "dev"], ["0.4", "0.4.1.2", "v0.4.1", "0.4.1-rc1", "latest", "٠.٤.١", "0.4.1\n", "", None]),
    "os.type": (["darwin", "windows", "linux"], ["macos", "beos", "MacOS", "", 1]),
    "host.arch": (["arm64", "amd64"], ["x86_64", "m4", "aarch64", "", 1]),
}


@pytest.mark.parametrize("key", sorted(_VALUES))
def test_the_allowlist_enforces_each_value_rule(key):
    spec = ATTRIBUTE_ALLOWLISTS[USAGE_EVENT][key]
    valid, invalid = _VALUES[key]
    assert all(spec.accepts(value) for value in valid) and not any(spec.accepts(value) for value in invalid)


def test_every_value_the_recorder_builds_passes_the_allowlist():
    specs = ATTRIBUTE_ALLOWLISTS[USAGE_EVENT]
    for version in ("0.4.1", "12.0.103", "0.4.1-12-gabc123", "", None, "dev"):
        assert specs["service.version"].accepts(release_tag(version)), version
    for moment in (NOW, NEXT_WEEK, datetime(2027, 1, 1, tzinfo=timezone.utc), datetime(2024, 12, 30, tzinfo=timezone.utc)):
        assert specs["corsia.usage.week"].accepts(iso_week(moment)), moment
    for key, value in platform_fields().items():
        assert specs[key].accepts(value)


def test_the_allowlist_takes_its_field_names_from_the_usage_field_list():
    assert set(ATTRIBUTE_ALLOWLISTS[USAGE_EVENT]) == set(FIELDS) == {"corsia.usage.week", "service.version", "os.type", "host.arch"}


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
