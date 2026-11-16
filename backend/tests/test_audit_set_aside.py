from __future__ import annotations

import json
import sqlite3

SINCE, UNTIL = "2026-11-09T00:00:00Z", "2026-11-16T00:00:00Z"
NOISE, NO_OWNER = "webhook.github.check_suite_completed", "webhook.github.pr_opened"
# Names an agent wrote: a declared event type and a script's own event.
AGENT_NAMED = ("acme-invoices.paid_late", "nightly-digest.failed")


def _seed(path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p1','P','t','t')")
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES ('steve-p1','steve','p1','A','t','t')"
        )
        db.executemany(
            "INSERT INTO dead_letter_events "
            "(id,internal_event,external_id,reason,payload_json,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("a1", NOISE, "ext-secret", "check_suite_noise_parked", '{"x":"payload-secret"}',
                 "parked", "2026-11-09T00:00:00+00:00"),
                ("a2", NOISE, None, "check_suite_noise_parked", "{}", "parked", "2026-11-09T23:59:59.5+00:00"),
                ("a3", NOISE, None, "check_suite_noise_parked", "{}", "parked", "2026-11-11T08:00:00Z"),
                # Another status still counts: it was set aside when it was written.
                ("b1", NO_OWNER, None, "no_owner", "{}", "retrying", "2026-11-10T12:00:00Z"),
                ("c1", AGENT_NAMED[0], None, "no_owner", "{}", "parked", "2026-11-12T01:00:00Z"),
                ("c2", AGENT_NAMED[1], None, "owner_inactive", "{}", "parked", "2026-11-12T02:00:00Z"),
                ("c3", "github.not_a_manifest_type", None, "no_owner", "{}", "parked", "2026-11-12T03:00:00Z"),
                # Outside the window on both sides; the end is exclusive.
                ("z1", NOISE, None, "check_suite_noise_parked", "{}", "parked", "2026-11-08T23:59:59+00:00"),
                ("z2", NOISE, None, "check_suite_noise_parked", "{}", "parked", "2026-11-16T00:00:00Z"),
            ],
        )


def test_set_aside_counts_per_kind_reason_and_day(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    result = audit_reads.read_audit_metric("steve-p1", "steve", "set_aside", since=SINCE, until=UNTIL)
    assert result["scope"] == "install"
    assert result["rows"] == [
        {"internal_event": NOISE, "reason": "check_suite_noise_parked", "total": 3,
         "days": {"2026-11-09": 2, "2026-11-11": 1}},
        {"internal_event": "declared", "reason": "no_owner", "total": 2, "days": {"2026-11-12": 2}},
        {"internal_event": "declared", "reason": "owner_inactive", "total": 1, "days": {"2026-11-12": 1}},
        {"internal_event": NO_OWNER, "reason": "no_owner", "total": 1, "days": {"2026-11-10": 1}},
    ]
    text = json.dumps(result)
    for private in ("secret", "steve", "ext-", *AGENT_NAMED, "not_a_manifest_type"):
        assert private not in text


def test_set_aside_names_kinds_from_code():
    from infra.audit_set_aside import _code_named
    from infra.failed_wake_surface import REPORT_EVENT

    named = _code_named()
    for kind in (NOISE, "github.pr.merged", "measurement.measurement.movement_digest", "worker.result",
                 REPORT_EVENT, "script.owner_gone", "github.ingress_overdue"):
        assert kind in named
    for kind in AGENT_NAMED:
        assert kind not in named


def test_set_aside_is_listed_by_the_tool():
    from tools.audit_reads import AuditReadTool

    tool = AuditReadTool()
    assert "set_aside" in tool.parameters["properties"]["metric"]["enum"]
    assert "set_aside counts" in tool.description


def _work_done(history_days: int) -> int:
    """SQLite steps for one set_aside call over a week, after `history_days` of older rows."""
    from datetime import datetime, timedelta, timezone

    from infra.audit_set_aside import set_aside

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE dead_letter_events (id TEXT PRIMARY KEY, internal_event TEXT NOT NULL, "
        "external_id TEXT, reason TEXT NOT NULL, payload_json TEXT, "
        "status TEXT NOT NULL DEFAULT 'parked', created_at TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX idx_dead_letter_status ON dead_letter_events(status, created_at)")
    end = datetime(2026, 11, 16, tzinfo=timezone.utc)
    minutes = range(-24 * 60 * (7 + history_days), 0, 10)
    db.executemany(
        "INSERT INTO dead_letter_events (id,internal_event,reason,status,created_at) VALUES (?,?,?,?,?)",
        [(f"{s}{m}", NOISE, "check_suite_noise_parked", s, (end + timedelta(minutes=m)).isoformat())
         for s in ("parked", "retrying") for m in minutes],
    )
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    db.set_progress_handler(tick, 100)
    set_aside(db, (end - timedelta(days=7)).isoformat(), end.isoformat())
    return steps[0]


def test_set_aside_work_follows_the_week_not_the_history():
    # Index ranges only: ninety more days of older rows add almost nothing.
    week_only, with_history = _work_done(0), _work_done(90)
    assert with_history < week_only * 1.1


async def test_restart_replay_poison_stores_a_code_and_keeps_the_text_on_the_delivery(isolated_db):
    from automation import restart_webhooks
    from infra.database import get_db

    with sqlite3.connect(isolated_db) as db:
        db.execute(
            "INSERT INTO webhook_deliveries (id,event_type,delivery_id,payload_hash,payload_json,"
            "status,created_at) VALUES ('w1','push','d1','h','[]','held_restart','2026-11-10T00:00:00Z')"
        )
    result = await restart_webhooks.replay_held_webhooks()
    assert result["dead_lettered"] == ["d1"]
    connection = get_db()
    try:
        dead = connection.execute("SELECT reason FROM dead_letter_events WHERE external_id='d1'").fetchone()
        delivery = connection.execute("SELECT error FROM webhook_deliveries WHERE delivery_id='d1'").fetchone()
    finally:
        connection.close()
    assert dead["reason"] == restart_webhooks.DEAD_LETTER_REASON == "restart_replay_poison"
    assert delivery["error"] == "stored webhook payload is not an object"


async def test_restart_replay_rows_read_under_one_github_name(isolated_db):
    """GitHub's raw header (``push``, ``issue_comment``) is neither code-named nor agent-declared."""
    from automation import restart_webhooks
    from infra.audit_set_aside import set_aside
    from infra.database import get_db

    with sqlite3.connect(isolated_db) as db:
        for row_id, header in (("w1", "push"), ("w2", "issue_comment"), ("w3", "pull_request_review")):
            db.execute(
                "INSERT INTO webhook_deliveries (id,event_type,delivery_id,payload_hash,payload_json,"
                "status,created_at) VALUES (?,?,?,'h','[]','held_restart','2026-11-10T00:00:00Z')",
                (row_id, header, f"d-{row_id}"),
            )
    result = await restart_webhooks.replay_held_webhooks()
    assert len(result["dead_lettered"]) == 3
    connection = get_db()
    try:
        rows = set_aside(connection, "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
    finally:
        connection.close()
    assert [(r["internal_event"], r["reason"], r["total"]) for r in rows] == [
        (restart_webhooks.DEAD_LETTER_KIND, "restart_replay_poison", 3),
    ]
