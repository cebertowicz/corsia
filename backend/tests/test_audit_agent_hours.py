from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from time import monotonic

import pytest

START = "2026-09-20T00:00:00Z"
END = "2026-09-21T00:00:00Z"


@pytest.fixture
def audit(isolated_db, monkeypatch):
    from infra import audit_reads
    from api.usage_rollup import definition

    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.executemany("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?, 't','t')",
                       [("p1", "PrivateProject"), ("p2", "SecondProject")])
        db.executemany(
            "INSERT INTO threads(id,user_id,project_id,title,spawned_by,agent_enabled,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?, 't','t')",
            [("caller", "owner", "p1", "SecretTitle", None, 1),
             ("worker", "owner", "p2", "WorkerTitle", "caller", 0),
             ("chat", "owner", None, "ChatTitle", None, 0),
             ("foreign", "foreign-user", "p1", "ForeignTitle", None, 1)])
        db.executemany("INSERT INTO vendor_accounts(id,connection_id,name,created_at,updated_at) VALUES (?, 'c', ?, 't','t')",
                       [("a", "Account A"), ("b", "Account B")])
        db.execute("INSERT OR REPLACE INTO llm_usage_hourly_state(id,definition,built_through) VALUES (1,?,?)",
                   (definition(), "2026-09-21T00"))
    def read(metric, since=START, until=END, rollup_definition=definition()):
        return audit_reads.read_audit_metric("caller", "owner", metric, since=since, until=until,
                                             rollup_definition=rollup_definition)
    return isolated_db, read


def add_usage(db, thread, hour, account="a", model="codex/model", count=1):
    db.execute("INSERT INTO llm_usage_hourly(hour,thread_id,model,source,vendor_account_id,admission,input_tier,idle_hours,"
               "records,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,"
               "fresh_tokens,fresh_unknown_records,cost_records,first_at) VALUES (?,?,?,'test',?,'x','x',0,?,?,?,?,?,?,?,0,0,?)",
               (hour, thread, model, account, count, 10*count, 2*count, 3*count, 4*count,
                19*count, 15*count, hour + ':00:00'))


def test_real_rollup_accounts_scope_and_privacy(audit):
    from api.usage_rollup import refresh_step
    path, read = audit
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO llm_usage(id,thread_id,vendor_account_id,model,source,prompt_tokens,completion_tokens,"
                       "cache_creation_tokens,cache_read_tokens,total_tokens,created_at) VALUES (?,?,?,'codex/x','test',10,2,3,4,19,?)",
                       [("u1", "caller", "a", "2026-09-20T01:01:00"),
                        ("u2", "caller", "b", "2026-09-20T01:02:00"),
                        ("u3", "worker", "a", "2026-09-20T02:01:00"),
                        ("u4", "chat", None, "2026-09-20T03:01:00"),
                        ("u5", "foreign", "a", "2026-09-20T04:01:00"),
                        ("u6", "deleted", "a", "2026-09-20T05:01:00")])
        db.execute("DELETE FROM llm_usage_hourly_state")
        while refresh_step(db, datetime(2026, 9, 21, 3)):
            pass
    result = read("agent_hours")
    rows = result["rows"]
    total = next(r for r in rows if r["row_type"] == "all")
    assert (total["agent_hours"], total["agents"], total["calls"]) == (3, 3, 4)
    assert (total["temporary_workers"], total["temporary_worker_hours"]) == (1, 1)
    assert (total["prompt"], total["completion"], total["cache_write"], total["cache_read"]) == (40, 8, 12, 16)
    assert next(r for r in rows if r["account_id"] == "a")["agent_hours"] == 2
    assert next(r for r in rows if r["account_id"] == "b")["agent_hours"] == 1
    assert next(r for r in rows if r["provider"] == "codex")["agent_hours"] == 1
    assert result["scope"] == "caller_user"
    text = json.dumps(result)
    for secret in ("caller", "worker", "chat", "foreign", "SecretTitle", "PrivateProject", "SecondProject", "thread_id", "project_id"):
        assert json.dumps(secret) not in text
    days = read("agent_day_levels")["rows"]
    assert {(r["kind"], r["location"]) for r in days} == {
        ("persistent_agent", "caller_project"), ("temporary_worker", "another_project"), ("other_chat", "no_project")}


def test_dirty_unbuilt_and_stale_coverage(audit):
    path, read = audit
    with sqlite3.connect(path) as db:
        add_usage(db, "caller", "2026-09-20T00")
        add_usage(db, "caller", "2026-09-20T01")
        db.execute("UPDATE llm_usage_hourly_state SET built_through='2026-09-20T23'")
        db.execute("INSERT INTO llm_usage_hourly_dirty(hour) VALUES ('2026-09-20T01')")
    result = read("agent_hours")
    assert (result["hours_in_window"], result["hours_left_out"]) == (24, 2)
    assert next(r for r in result["rows"] if r["row_type"] == "all")["calls"] == 1
    day = read("agent_day_levels")
    assert (day["days_in_window"], day["days_left_out"], day["rows"]) == (1, 1, [])
    for definition in (None, "stale"):
        missing = read("agent_hours", rollup_definition=definition)
        assert missing["hours_left_out"] == 24 and missing["rows"] == []
        missing_days = read("agent_day_levels", rollup_definition=definition)
        assert missing_days["days_left_out"] == 1 and missing_days["rows"] == []


def test_activity_band_edges(audit):
    path, read = audit
    with sqlite3.connect(path) as db:
        for n in (2, 3, 6, 7, 12, 13, 18, 19, 24):
            name = f"band{n}"
            db.execute("INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,'owner','p1','t','t')", (name,))
            for hour in range(n):
                add_usage(db, name, f"2026-09-20T{hour:02d}")
    rows = read("agent_day_levels")["rows"]
    assert {r["band"]: (r["agent_days"], r["agent_hours"]) for r in rows} == {
        "1-2": (1, 2), "3-6": (2, 9), "7-12": (2, 19), "13-18": (2, 31), "19-24": (2, 43)}


@pytest.mark.parametrize("metric,since,until,fix", [
    ("agent_hours", "2026-09-20T00:01:00Z", END, "whole UTC hours"),
    ("agent_day_levels", "2026-09-20T01:00:00Z", END, "UTC midnight"),
    ("agent_day_levels", "2026-09-20T00:01:00Z", END, "UTC midnight"),
])
def test_alignment_errors(audit, metric, since, until, fix):
    from infra.audit_reads import AuditReadError
    _, read = audit
    with pytest.raises(AuditReadError) as error:
        read(metric, since=since, until=until)
    assert error.value.category == "invalid_window"
    assert fix in error.value.detail and "2026-01-01T00:00:00Z" in error.value.detail


def test_limits_apply_to_results(audit, monkeypatch):
    from infra import audit_reads
    path, read = audit
    with sqlite3.connect(path) as db:
        for n in range(50):
            add_usage(db, "caller", "2026-09-20T00", account=f"account-{n}")
    with pytest.raises(audit_reads.AuditReadError) as error:
        read("agent_hours")
    assert error.value.category == "result_limit"
    monkeypatch.setattr(audit_reads, "MAX_ROWS", 100)
    monkeypatch.setattr(audit_reads, "MAX_BYTES", 100)
    with pytest.raises(audit_reads.AuditReadError) as error:
        read("agent_hours")
    assert error.value.category == "result_limit"


@pytest.mark.asyncio
async def test_tool_registry_passes_definition(audit):
    from infra import state
    from tools.audit_reads import AuditReadTool
    from tools.registry import ToolRegistry
    path, _ = audit
    with sqlite3.connect(path) as db:
        add_usage(db, "caller", "2026-09-20T00")
    tool = AuditReadTool()
    assert {"agent_hours", "agent_day_levels"} <= set(tool.parameters["properties"]["metric"]["enum"])
    assert "agent_hours" in tool.description and "agent_day_levels" in tool.description
    registry = ToolRegistry()
    registry.register(tool)
    token = state._current_thread_id.set("caller")
    try:
        result = json.loads(await registry.execute("audit_read", {"metric": "agent_hours", "since": START, "until": END}))
    finally:
        state._current_thread_id.reset(token)
    assert result["hours_left_out"] == 0
    assert next(r for r in result["rows"] if r["row_type"] == "all")["calls"] == 1


def test_http_route_passes_definition(audit):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth_fixtures import OWNER_TOKEN, OWNER_USER
    from api.audit_reads import router
    path, _ = audit
    with sqlite3.connect(path) as db:
        db.execute("UPDATE threads SET user_id=? WHERE id='caller'", (OWNER_USER,))
        add_usage(db, "caller", "2026-09-20T00")
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/audit/threads/caller/metrics/agent_hours", params={"since": START, "until": END},
                                   headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
    assert response.status_code == 200, response.text
    assert response.json()["hours_left_out"] == 0
    assert next(r for r in response.json()["rows"] if r["row_type"] == "all")["calls"] == 1


def test_scale_through_real_reader(audit):
    """A week of rollup rows at today's order of size reads inside the deadline."""
    path, read = audit
    total = 50_000
    start = datetime(2026, 9, 20)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE llm_usage_hourly_state SET built_through='2026-09-27T00'")
        db.executemany(
            "INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,'owner','p1','t','t')",
            [(f"scale-{i}",) for i in range(100)],
        )
        db.executemany(
            "INSERT INTO llm_usage_hourly(hour,thread_id,model,source,vendor_account_id,admission,input_tier,"
            "idle_hours,records,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,"
            "total_tokens,fresh_tokens,fresh_unknown_records,cost_records,first_at) "
            "VALUES (?,?,?,'test','a','x','x',0,1,10,2,3,4,19,15,0,0,'t')",
            (((start + timedelta(hours=hour)).isoformat()[:13], f"scale-{i % 100}", f"codex/model-{i // 4200}")
             for hour in range(168) for i in range(hour, total, 168)),
        )
    for metric in ("agent_hours", "agent_day_levels"):
        began = monotonic()
        result = read(metric, until="2026-09-27T00:00:00Z")
        print(f"SCALE {total} {metric} {monotonic() - began:.4f}s")
        assert result["hours_left_out"] == 0
        assert sum(r["calls"] for r in result["rows"] if r.get("row_type") != "all") == total
