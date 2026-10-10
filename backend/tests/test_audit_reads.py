from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest


def _seed(path) -> None:
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            [("p1", "Shared", "t", "t"), ("p2", "Other", "t", "t")],
        )
        db.executemany(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("steve-p1", "steve", "p1", "A", "t", "t"),
                ("leksi-p1", "leksi", "p1", "B", "t", "t"),
                ("steve-p2", "steve", "p2", "C", "t", "t"),
            ],
        )
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("m1", "steve-p1", "user", "caller message", "t", 1),
                ("m2", "leksi-p1", "user", "FOREIGN SECRET" * 100, "t", 1),
                ("m3", "steve-p2", "user", "OTHER PROJECT" * 100, "t", 1),
            ],
        )
        db.executemany(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES (?,?,0,0,'origin','idle','cold','installed',"
            "'x','x','x','x',0,'[]','t')",
            [("seed-c1", "steve-p1"), ("seed-c2", "leksi-p1"),
             ("seed-c3", "steve-p2")],
        )


def test_stored_message_pressure_only_returns_caller_user_and_project(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    result = audit_reads.read_audit_metric("steve-p1", "steve", "stored_message_pressure")
    assert result["scope"] == "caller_user_and_project"
    assert [row["thread_id"] for row in result["rows"]] == ["steve-p1"]
    assert "caller message" not in json.dumps(result)
    assert "FOREIGN SECRET" not in json.dumps(result)

    with pytest.raises(audit_reads.AuditReadError, match="caller-owned") as wrong_user:
        audit_reads.read_audit_metric("steve-p1", "leksi", "stored_message_pressure")
    assert wrong_user.value.category == "scope_denied"
    assert audit_reads.read_audit_metric("steve-p2", "steve", "stored_message_pressure")["rows"][0]["thread_id"] == "steve-p2"


def test_no_checkpoint_is_labeled_as_stored_history(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES ('uncheckpointed','steve','p1','D','t','t')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('uncheckpointed-msg','uncheckpointed','user',?, 't',1)",
            ("huge uncheckpointed message" * 500,),
        )
    rows = audit_reads.read_audit_metric("steve-p1", "steve", "stored_message_pressure")["rows"]
    assert [row["thread_id"] for row in rows] == ["uncheckpointed", "steve-p1"]
    assert rows[0]["checkpoint_state"] == "no_checkpoint"
    assert rows[0]["stored_messages"] == 1
    assert rows[1]["checkpoint_state"] == "installed"


def test_pressure_ranking_discloses_top_ten_limit(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.executemany(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES (?,'steve','p1','T','t','t')",
            [(f"rank-{i}",) for i in range(11)],
        )
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?,'user',?,'t',1)",
            [(f"rank-msg-{i}", f"rank-{i}", "x" * (100 + i * 4))
             for i in range(11)],
        )
    result = audit_reads.read_audit_metric(
        "steve-p1", "steve", "stored_message_pressure"
    )
    assert result["rank_limit"] == 10
    assert [row["thread_id"] for row in result["rows"]] == [
        f"rank-{i}" for i in range(10, 0, -1)
    ]


def test_pressure_activity_window_is_explicit_subset(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "UPDATE threads SET updated_at='2026-09-22T12:00:00+00:00' "
            "WHERE id='steve-p1'"
        )
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES ('older','steve','p1','Old','t','2026-09-21T12:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('older-msg','older','user',?, 't',1)", ("old" * 1000,)
        )
    global_rows = audit_reads.read_audit_metric(
        "steve-p1", "steve", "stored_message_pressure"
    )["rows"]
    assert [row["thread_id"] for row in global_rows] == ["older", "steve-p1"]
    recent = audit_reads.read_audit_metric(
        "steve-p1", "steve", "stored_message_pressure",
        since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
    )
    assert recent["window"] == [
        "2026-09-22T00:00:00+00:00", "2026-09-23T00:00:00+00:00"
    ]
    assert [row["thread_id"] for row in recent["rows"]] == ["steve-p1"]
    with pytest.raises(audit_reads.AuditReadError) as half_window:
        audit_reads.read_audit_metric(
            "steve-p1", "steve", "stored_message_pressure",
            since="2026-09-22T00:00:00Z",
        )
    assert half_window.value.category == "invalid_window"


@pytest.mark.parametrize("state,reason", [("purged", None), ("archived", "user")])
def test_unavailable_actor_is_denied(isolated_db, monkeypatch, state, reason):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "UPDATE threads SET lifecycle_state=?, archived_reason=? WHERE id='steve-p1'",
            (state, reason),
        )
    for metric in audit_reads.AUDIT_METRICS:
        with pytest.raises(audit_reads.AuditReadError) as denied:
            audit_reads.read_audit_metric(
                "steve-p1", "steve", metric,
                **({} if metric == "stored_message_pressure" else {
                    "since": "2026-09-22T00:00:00Z", "until": "2026-09-23T00:00:00Z"
                }),
            )
        assert denied.value.category == "scope_denied"


def test_stored_message_pressure_uses_available_threads_and_current_epoch(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.execute("UPDATE threads SET history_epoch=1 WHERE id='steve-p1'")
        db.execute("UPDATE messages SET seq=2 WHERE id='m1'")
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('m4','steve-p1','user','new tail','t',3)"
        )
        db.executemany(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at,"
            "lifecycle_state,archived_reason) VALUES (?,'steve','p1','A','t','t','archived',?)",
            [("steve-user-archive", "user"), ("steve-dormant", "dormancy")],
        )
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?, 'user', ?, 't', 1)",
            [
                ("archived-msg", "steve-user-archive", "archived" * 100),
                ("dormant-msg", "steve-dormant", "dormant" * 100),
            ],
        )
        db.executemany(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES (?,'steve-p1',?,?, 'm1','idle','cold','installed',"
            "'x','x','x','x',0,'[]','2026-09-22T01:00:00+00:00')",
            [("old-epoch", 0, 100), ("current-epoch", 1, 2)],
        )
        db.execute(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES ('dormant-c','steve-dormant',0,0,'origin','idle','cold',"
            "'installed','x','x','x','x',0,'[]','t')"
        )
    rows = audit_reads.read_audit_metric("steve-p1", "steve", "stored_message_pressure")["rows"]
    assert [row["thread_id"] for row in rows] == ["steve-dormant", "steve-p1"]
    assert rows[1]["stored_messages"] == 1


def test_metric_window_and_output_caps(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    empty = audit_reads.read_audit_metric(
        "steve-p1", "steve", "lease_reviews",
        since="2026-09-01T00:00:00Z", until="2026-09-02T00:00:00Z",
    )
    assert empty["rows"] == []
    assert empty["scope"] == "caller_user_and_project"
    with pytest.raises(audit_reads.AuditReadError) as long_window:
        audit_reads.read_audit_metric(
            "steve-p1", "steve", "checkpoint_health",
            since="2026-09-01T00:00:00Z", until="2026-09-09T00:00:00Z",
        )
    assert long_window.value.category == "invalid_window"
    with pytest.raises(audit_reads.AuditReadError) as unexpected_window:
        audit_reads.read_audit_metric(
            "steve-p1", "steve", "stored_message_pressure",
            since="2026-09-01T00:00:00Z",
        )
    assert unexpected_window.value.category == "invalid_window"
    monkeypatch.setattr(audit_reads, "MAX_BYTES", 10)
    with pytest.raises(audit_reads.AuditReadError) as too_large:
        audit_reads.read_audit_metric("steve-p1", "steve", "stored_message_pressure")
    assert too_large.value.category == "result_limit"


@pytest.mark.asyncio
async def test_agent_tool_uses_injected_thread_identity(isolated_db, monkeypatch):
    from infra import audit_reads
    from tools.audit_reads import AuditReadTool

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    result = json.loads(await AuditReadTool().execute(
        metric="stored_message_pressure", _thread_id="steve-p1", _thread_project_id="p2"
    ))
    assert [row["thread_id"] for row in result["rows"]] == ["steve-p1"]
    denied = json.loads(await AuditReadTool().execute(metric="stored_message_pressure"))
    assert denied["error"] == "scope_denied"


@pytest.mark.asyncio
async def test_real_tool_registry_ignores_forged_scope(isolated_db, monkeypatch):
    from infra import audit_reads, state
    from tools.audit_reads import AuditReadTool
    from tools.registry import ToolRegistry

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    registry = ToolRegistry()
    registry.register(AuditReadTool())
    thread_token = state._current_thread_id.set("steve-p1")
    try:
        result = json.loads(await registry.execute(
            "audit_read", {"metric": "stored_message_pressure", "_thread_id": "leksi-p1"}
        ))
    finally:
        state._current_thread_id.reset(thread_token)
    assert [row["thread_id"] for row in result["rows"]] == ["steve-p1"]


def test_checkpoint_health_counts_only_owned_rows(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at,"
            "lifecycle_state,archived_reason) VALUES "
            "('archived-health','steve','p1','A','t','t','archived','user')"
        )
        db.executemany(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES (?,?,0,1,'m','idle','cold',?,'x','x','x','x',0,'[]',?)",
            [
                ("hidden-health", "archived-health", "failed", "2026-09-22T01:00:00+00:00"),
                ("c1", "steve-p1", "failed", "2026-09-22T01:00:00+00:00"),
                ("c2", "leksi-p1", "failed", "2026-09-22T01:00:00+00:00"),
                ("c3", "steve-p2", "failed", "2026-09-22T01:00:00+00:00"),
                ("c4", "steve-p1", "installed", "2026-09-22T02:00:00+00:00"),
            ],
        )
    result = audit_reads.read_audit_metric(
        "steve-p1", "steve", "checkpoint_health",
        since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
    )
    assert result["rows"] == [
        {"trigger": "idle", "status": "failed", "n": 1, "recovered": 1, "unrecovered": 0},
        {"trigger": "idle", "status": "installed", "n": 1, "recovered": 0, "unrecovered": 0},
    ]


def test_checkpoint_health_unrecovered_failure_has_zero_recovered(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES ('failed-no-install', 'steve-p1', 0, 1, 'm', 'idle', 'cold', "
            "'failed', 'x', 'x', 'x', 'x', 0, '[]', '2026-09-22T01:00:00')"
        )

    result = audit_reads.read_audit_metric(
        "steve-p1", "steve", "checkpoint_health",
        since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
    )
    assert result["rows"] == [
        {"trigger": "idle", "status": "failed", "n": 1, "recovered": 0, "unrecovered": 1},
    ]


def test_checkpoint_health_aggregates_large_scoped_history_within_deadline(
    isolated_db, monkeypatch
):
    """Avoid one later-checkpoint scan for every failed checkpoint."""
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.executemany(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES (?, 'steve-p1', 0, 1, 'm', 'idle', 'cold', 'failed', "
            "'x', 'x', 'x', 'x', 0, '[]', '2026-09-22T01:00:00')",
            [(f"failed-{index}",) for index in range(20_000)],
        )
        db.execute(
            "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,continuity_base_revision,"
            "continuity_base_snapshot_json,started_at) "
            "VALUES ('installed-after-failures', 'steve-p1', 0, 1, 'm', 'idle', "
            "'cold', 'installed', 'x', 'x', 'x', 'x', 0, '[]', "
            "'2026-09-22T02:00:00')"
        )

    result = audit_reads.read_audit_metric(
        "steve-p1", "steve", "checkpoint_health",
        since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
    )

    assert result["rows"] == [
        {"trigger": "idle", "status": "failed", "n": 20_000, "recovered": 20_000,
         "unrecovered": 0},
        {"trigger": "idle", "status": "installed", "n": 1, "recovered": 0,
         "unrecovered": 0},
    ]


@pytest.mark.parametrize(
    "legacy_category", [None, 42, "PRIVATE-CONTENT-IN-OLD-RECORD", "unclear_scope"]
)
def test_lease_review_metric_never_returns_untrusted_category(
    isolated_db, monkeypatch, legacy_category
):
    from infra import audit_reads
    from infra.host_access_leases import record_application_review, request_lease

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.row_factory = sqlite3.Row
        requested = request_lease(
            "steve-p1", purpose="Read audit", plan=["Read"], duration_seconds=60,
            now=datetime(2026, 9, 22, 12), db=db,
        )
        record_application_review(
            requested["id"],
            expected_revision=requested["revision"],
            application_hash=requested["application_hash"],
            allowed=False,
            reviewer_model="native/reviewer",
            review_result={
                "reason": "PRIVATE-CONTENT-IN-OLD-RECORD",
                **({"reason_category": legacy_category} if legacy_category else {}),
            },
            now=datetime(2026, 9, 22, 12), db=db,
        )
    result = audit_reads.read_audit_metric(
        "steve-p1", "steve", "lease_reviews",
        since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
    )
    expected_category = "unclear_scope" if legacy_category == "unclear_scope" else "unknown"
    assert result["rows"] == [
        {"state": "rejected", "reason_category": expected_category, "n": 1}
    ]
    assert "PRIVATE-CONTENT" not in json.dumps(result)
    if legacy_category is None:
        with sqlite3.connect(isolated_db) as db:
            db.execute(
                "UPDATE host_access_leases SET application_review_result=? WHERE id=?",
                ("PRIVATE-MALFORMED-REVIEW{", requested["id"]),
            )
        malformed = audit_reads.read_audit_metric(
            "steve-p1", "steve", "lease_reviews",
            since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
        )
        assert malformed["rows"] == [
            {"state": "rejected", "reason_category": "unknown", "n": 1}
        ]
        assert "PRIVATE-MALFORMED" not in json.dumps(malformed)
    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES ('steve-live','steve','p1','Live','t','t')"
        )
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', archived_reason='user' "
            "WHERE id='steve-p1'"
        )
    hidden = audit_reads.read_audit_metric(
        "steve-live", "steve", "lease_reviews",
        since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
    )
    assert hidden["rows"] == []


def test_missing_audit_store_returns_unavailable(tmp_path, monkeypatch):
    from infra import audit_reads

    monkeypatch.setattr(audit_reads, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises(audit_reads.AuditReadError) as unavailable:
        audit_reads.read_audit_metric("caller", "owner", "stored_message_pressure")
    assert unavailable.value.category == "unavailable"
    assert "reviewed lease" in unavailable.value.detail


def test_audit_telemetry_records_only_metric_and_outcome(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    events = []
    monkeypatch.setattr(
        audit_reads, "emit_product_event",
        lambda category, action, **kwargs: events.append((category, action, kwargs)),
    )
    audit_reads.read_audit_metric("steve-p1", "steve", "stored_message_pressure")
    with pytest.raises(audit_reads.AuditReadError):
        audit_reads.read_audit_metric("leksi-p1", "steve", "stored_message_pressure")
    assert events == [
        ("audit_read", "succeeded", {"properties": {"metric": "stored_message_pressure"}}),
        ("audit_read", "denied", {"properties": {
            "metric": "stored_message_pressure", "category": "scope_denied"
        }}),
    ]


def test_audit_query_deadline_interrupts_large_read(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    with sqlite3.connect(isolated_db) as db:
        db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,'steve-p1','user','x','t',?)",
            [(f"bulk-{i}", i + 2) for i in range(3000)],
        )
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    monkeypatch.setattr(audit_reads, "TIMEOUT_SECONDS", 0)
    with pytest.raises(audit_reads.AuditReadError) as timed_out:
        audit_reads.read_audit_metric("steve-p1", "steve", "stored_message_pressure")
    assert timed_out.value.category == "timeout"
    assert "since and until" in timed_out.value.detail
    with pytest.raises(audit_reads.AuditReadError) as narrowed_timeout:
        audit_reads.read_audit_metric(
            "steve-p1", "steve", "stored_message_pressure",
            since="2026-09-22T00:00:00Z", until="2026-09-23T00:00:00Z",
        )
    assert narrowed_timeout.value.category == "timeout"
    assert "narrow the activity window" in narrowed_timeout.value.detail


def test_http_route_authenticates_and_refuses_foreign_thread(isolated_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth_fixtures import OWNER_TOKEN
    from api.audit_reads import router
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    url = "/api/audit/threads/steve-p1/metrics/stored_message_pressure"
    assert client.get(url).status_code == 401
    owned = client.get(url, headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
    assert owned.status_code == 200
    assert owned.json()["rows"][0]["thread_id"] == "steve-p1"
    foreign = client.get(
        "/api/audit/threads/leksi-p1/metrics/stored_message_pressure",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["category"] == "scope_denied"
    monkeypatch.setattr(audit_reads, "TIMEOUT_SECONDS", 0)
    timeout = client.get(url, headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
    assert timeout.status_code == 503
    assert timeout.json()["detail"]["category"] == "timeout"
    assert "since and until" in timeout.json()["detail"]["next"]
    narrowed = client.get(
        url, headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        params={"since": "2026-09-22T00:00:00Z", "until": "2026-09-23T00:00:00Z"},
    )
    assert narrowed.status_code == 503
    assert "narrow the activity window" in narrowed.json()["detail"]["next"]
