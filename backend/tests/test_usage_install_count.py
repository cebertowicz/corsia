"""Install count: local weekly recording, the closed summary, and totals-only intake."""
from __future__ import annotations

import ast
import json
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.usage_fields import USAGE_EVENT
from infra.database import initialize_schema
from infra.usage_intake import admit_summaries, admit_summary
from infra.usage_summary import SummaryError, build_summary, iso_week, record_install_week, release_tag, validate_summary

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
FIELDS = {"os": "macos", "chip": "arm64"}
ID_A, ID_B, ID_C = ("a" * 31 + c for c in "abc")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    initialize_schema(connection)
    yield connection
    connection.close()


def _totals(db):
    return [tuple(row) for row in db.execute("SELECT week,release_tag,os,chip,install_weeks FROM usage_install_totals ORDER BY 1,2,3,4")]


def test_iso_week_and_release_tag():
    assert iso_week(NOW) == "2026-W44"
    assert iso_week(datetime(2027, 1, 1, tzinfo=timezone.utc)) == "2026-W53"
    assert release_tag("0.4.1") == "0.4.1"
    assert [release_tag(v) for v in ("0.4.1-12-gabc123", "", None, "dev", "v0.4.1")] == ["dev"] * 5


def test_recording_is_once_per_week_and_local(db):
    assert record_install_week(db, now=NOW, version="0.4.1", fields=FIELDS) is True
    assert record_install_week(db, now=NOW, version="0.4.2", fields=FIELDS) is False
    summary = build_summary(db, "2026-W44")
    assert summary is not None and summary == {"week": "2026-W44", "release": "0.4.1", "os": "macos", "chip": "arm64", "delivery_id": summary["delivery_id"]}
    assert build_summary(db, "2026-W44") == summary  # a rebuild carries the same id, so a retry is recognised
    assert build_summary(db, "2026-W43") is None
    row = db.execute("SELECT source,provenance FROM outcome_events WHERE event_name=?", (USAGE_EVENT,)).fetchone()
    assert tuple(row) == ("corsia", "instrumented")


def test_an_install_left_open_across_a_week_records_both_weeks(db, monkeypatch):
    import infra.usage_summary as usage

    calls = []
    real = usage.record_install_week
    monkeypatch.setattr(usage, "_recorded_week", None)
    monkeypatch.setattr("infra.database.get_db", lambda: db)
    monkeypatch.setattr("infra.build_info.app_version", lambda: "0.4.1")
    monkeypatch.setattr(usage, "record_install_week", lambda *a, **k: calls.append(k["now"]) or real(*a, **k))
    week_n, week_after = NOW, datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    assert usage.install_week_due(week_n)
    usage.record_current_install_week(week_n)
    usage.record_current_install_week(week_n)  # same week: no second DB call
    assert not usage.install_week_due(week_n) and usage.install_week_due(week_after)
    usage.record_current_install_week(week_after)  # same process, a week later
    assert len(calls) == 2
    weeks = [r[0] for r in db.execute("SELECT dedupe_key FROM outcome_events WHERE event_name=? ORDER BY 1", (USAGE_EVENT,))]
    assert weeks == [f"{USAGE_EVENT}:2026-W44", f"{USAGE_EVENT}:2026-W45"]


def test_unlisted_machine_leaves_os_and_chip_out(db):
    record_install_week(db, now=NOW, version=None, fields={})
    summary = build_summary(db, "2026-W44")
    assert summary == {"week": "2026-W44", "release": "dev", "delivery_id": summary["delivery_id"]}


@pytest.mark.parametrize("extra", [{"user": "x"}, {"host": "h"}, {"locale": "en"}, {"thread_id": "t"}])
def test_summary_with_any_other_field_fails(extra):
    with pytest.raises(SummaryError, match="outside the closed list"):
        validate_summary({"week": "2026-W44", "release": "dev", "delivery_id": ID_A, **extra})


def test_building_fails_on_a_stored_row_with_an_extra_field(db):
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,attributes) "
        "VALUES ('1',?,?,'corsia','instrumented','t','t',?)",
        (f"{USAGE_EVENT}:2026-W44", USAGE_EVENT, json.dumps({"week": "2026-W44", "release": "dev", "delivery_id": ID_A, "path": "/x"})),
    )
    with pytest.raises(SummaryError):
        build_summary(db, "2026-W44")


@pytest.mark.parametrize("body", [
    "text", ["x"], {}, {"week": "2026-W44", "release": "dev"},
    {"week": "2026-W99", "release": "dev", "delivery_id": ID_A},
    {"week": "2026-W44", "release": "latest", "delivery_id": ID_A},
    {"week": "2026-W44", "release": "dev", "delivery_id": ID_A, "os": "beos"},
    {"week": "2026-W44", "release": "dev", "delivery_id": ID_A, "chip": "m4"},
    {"week": "2026-W44", "release": "dev", "delivery_id": "A" * 32},
    {"week": "2026-W44", "release": "dev", "delivery_id": "host-name"},
    {"week": "2026-W44", "release": "dev", "delivery_id": 7},
])
def test_intake_refuses_and_counts_anything_outside_the_schema(db, body):
    result = admit_summary(db, body)
    assert not result.accepted and result.refusal
    assert _totals(db) == []
    counted = db.execute("SELECT n FROM outcome_event_counters WHERE counter='record_refused' AND event_name=?", (USAGE_EVENT,)).fetchone()
    assert counted[0] == 1


def test_intake_keeps_totals_only(db):
    bodies = [
        {"week": "2026-W44", "release": "0.4.1", "delivery_id": ID_A, **FIELDS},
        {"week": "2026-W44", "release": "0.4.1", "delivery_id": ID_B, **FIELDS},
        {"week": "2026-W44", "release": "dev", "delivery_id": ID_C},
        {"week": "2026-W44", "release": "dev", "delivery_id": ID_C, "user": "someone"},
    ]
    result = admit_summaries(db, bodies)
    assert (result.accepted, result.refused, result.duplicates) == (3, 1, 0)
    assert _totals(db) == [("2026-W44", "0.4.1", "macos", "arm64", 2), ("2026-W44", "dev", "", "", 1)]
    assert db.execute("SELECT count(*) FROM outcome_events").fetchone()[0] == 0  # no upload's own row
    assert [r[0] for r in db.execute("SELECT delivery_id FROM usage_admitted_deliveries ORDER BY 1")] == [ID_A, ID_B, ID_C]


def test_admitting_the_same_body_twice_counts_once(db):
    body = {"week": "2026-W44", "release": "0.4.1", "delivery_id": ID_A, **FIELDS}
    assert admit_summary(db, body).accepted
    again = admit_summary(db, dict(body))
    assert not again.accepted and again.duplicate and again.refusal is None
    assert _totals(db) == [("2026-W44", "0.4.1", "macos", "arm64", 1)]
    assert db.execute("SELECT count(*) FROM outcome_event_counters WHERE counter='record_refused'").fetchone()[0] == 0  # a repeat is not a refusal
    batch = admit_summaries(db, [body, {**body, "delivery_id": ID_B}])
    assert (batch.accepted, batch.refused, batch.duplicates) == (1, 0, 1)
    assert _totals(db) == [("2026-W44", "0.4.1", "macos", "arm64", 2)]


def test_a_retried_upload_of_the_recorded_summary_counts_once(db):
    record_install_week(db, now=NOW, version="0.4.1", fields=FIELDS)
    first, retry = build_summary(db, "2026-W44"), build_summary(db, "2026-W44")
    assert admit_summary(db, first).accepted and admit_summary(db, retry).duplicate
    assert _totals(db) == [("2026-W44", "0.4.1", "macos", "arm64", 1)]


def test_delivery_ids_are_random_per_summary_and_link_nothing_across_weeks(db):
    ids = []
    for moment in (NOW, datetime(2026, 11, 3, 12, tzinfo=timezone.utc)):
        record_install_week(db, now=moment, version="0.4.1", fields=FIELDS)
        ids.append(build_summary(db, iso_week(moment))["delivery_id"])
    assert ids[0] != ids[1] and all(len(i) == 32 for i in ids)


def test_collection_intake_and_build_make_no_network_call(db, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("usage collection touched the network")

    for name in ("create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    record_install_week(db, now=NOW, version="0.4.1", fields=FIELDS)
    summary = build_summary(db, "2026-W44")
    assert admit_summary(db, summary).accepted
    assert _totals(db) == [("2026-W44", "0.4.1", "macos", "arm64", 1)]
    assert admit_summary(db, summary).duplicate  # the repeat is recognised too


def test_usage_modules_import_no_network_or_model_client():
    banned = {"socket", "ssl", "http", "urllib", "urllib3", "httpx", "requests", "aiohttp", "websockets", "litellm", "openai", "anthropic"}
    for module in ("usage_summary", "usage_intake"):
        tree = ast.parse((Path(__file__).resolve().parent.parent / "infra" / f"{module}.py").read_text())
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not names & banned, module
