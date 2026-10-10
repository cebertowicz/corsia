"""log_read: conductor gating, filters, and the response-size bound."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from infra.conductor import WORKSPACE_SCOPE, set_conductor
from infra.database import get_db
from infra.otel_logs.capture import CapturedRecord
from infra.otel_logs.store import LogStore
from tests.registry_helpers import make_session
from tools import log_read
from tools.catalog import TOOL_CATALOG, is_reachable
from tools.log_read import MAX_RESPONSE_BYTES, LogReadTool, bounded_response


@pytest.fixture
def threads(isolated_db):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            ("logs-project", "Logs project", "t", "t"),
        )
        db.commit()
        workspace = make_session(db, prefix="ws-conductor")
        project = make_session(db, project_id="logs-project", prefix="p-conductor")
        ordinary = make_session(db, project_id="logs-project", prefix="ordinary")
        assert set_conductor(WORKSPACE_SCOPE, workspace, db=db)
        assert set_conductor("logs-project", project, db=db)
        db.commit()
        return workspace, project, ordinary
    finally:
        db.close()


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "otel_logs.db"
    store = LogStore(path, disk_floor_bytes=0)
    assert store.open()
    now = time.time_ns()
    store.insert(
        [
            CapturedRecord(now - 3_000_000_000, 9, "INFO", "core.turn", "turn started", None, "run", "backend/core/turn.py", 10),
            CapturedRecord(now - 2_000_000_000, 13, "WARNING", "infra.database", "database is locked", None, "w", "backend/infra/database.py", 20),
            CapturedRecord(
                now - 1_000_000_000, 17, "ERROR", "core.turn", "turn failed",
                "Traceback (most recent call last):\n  File x\nKeyError: 'thread'", "run", "backend/core/turn.py", 30,
            ),
        ],
        now,
    )
    store.close()
    monkeypatch.setattr(log_read, "default_store_path", lambda: path)
    return path


async def _call(thread_id: str, **params) -> str:
    return await LogReadTool().execute(_thread_id=thread_id, **params)


@pytest.mark.asyncio
async def test_every_conductor_reads_and_ordinary_threads_are_refused(threads, store_path):
    workspace, project, ordinary = threads
    for conductor in (workspace, project):
        result = json.loads(await _call(conductor))
        assert [r["message"] for r in result["records"]] == ["turn failed", "database is locked"]
    refused = await _call(ordinary)
    assert refused.startswith("Error: not_conductor")
    assert "do not retry" in refused and "host lease" in refused
    assert "database is locked" not in refused


@pytest.mark.asyncio
async def test_records_carry_the_documented_fields_and_filters(threads, store_path):
    workspace, _project, _ordinary = threads
    result = json.loads(await _call(workspace, min_level="INFO", logger="core.turn"))
    newest, oldest = result["records"]
    assert newest["level"] == "ERROR" and newest["at"] == "backend/core/turn.py:30"
    assert newest["exception"].endswith("KeyError: 'thread'")
    assert newest["time"].endswith("Z")
    assert oldest["message"] == "turn started" and oldest["exception"] is None
    assert result["more_matching"] is False and result["truncated_for_size"] is False
    only = json.loads(await _call(workspace, contains="LOCKED"))
    assert [r["message"] for r in only["records"]] == ["database is locked"]
    limited = json.loads(await _call(workspace, min_level="INFO", limit=1))
    assert len(limited["records"]) == 1 and limited["more_matching"] is True
    assert "until=" in limited["note"]
    past = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    assert json.loads(await _call(workspace, until=past))["records"] == []


@pytest.mark.asyncio
async def test_invalid_arguments_are_actionable(threads, store_path):
    workspace = threads[0]
    assert (await _call(workspace, since="yesterday")).startswith("Error: since must be an ISO 8601")
    assert "earlier than until" in await _call(
        workspace, since="2026-09-28T02:00:00Z", until="2026-09-28T01:00:00Z",
    )
    assert "min_level" in await _call(workspace, min_level="DEBUG")


@pytest.mark.asyncio
async def test_missing_store_reports_capture_state(threads, tmp_path, monkeypatch):
    monkeypatch.setattr(log_read, "default_store_path", lambda: tmp_path / "none.db")
    result = await _call(threads[0])
    assert result.startswith("Error: store_unavailable")


def test_response_is_bounded_and_says_so():
    rows = [
        {
            "ts_ns": 1_000_000_000 * (10_000 - n), "severity_text": "ERROR",
            "logger": "core.turn", "body": "m" * 5_000, "exception": "t" * 5_000,
            "function": "f", "file_path": "backend/core/turn.py", "line": n,
        }
        for n in range(101)
    ]
    text = bounded_response(rows, 100, ["a", "b"], {"state": "on"})
    result = json.loads(text)
    assert len(text.encode("utf-8")) <= MAX_RESPONSE_BYTES
    assert result["truncated_for_size"] is True and result["more_matching"] is True
    assert 0 < len(result["records"]) < 100
    assert len(result["records"][0]["message"]) < 1_600
    assert result["note"].startswith(f"Showing {len(result['records'])} newest")
    assert f"until={result['records'][-1]['time']}" in result["note"]


def test_log_read_is_catalogued_in_the_host_pack():
    entry = TOOL_CATALOG["log_read"]
    assert entry.authority == "conductor-log-read" and not entry.core
    assert "other threads receive a final not_conductor refusal" in entry.summary
    assert is_reachable("log_read")
    description = LogReadTool().description
    assert description.startswith("Conductors only.")
    assert "not_conductor" in description and "backend.log" in description
