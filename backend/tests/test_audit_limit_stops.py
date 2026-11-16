from __future__ import annotations

import json
import sqlite3

FIVE = "anthropic:five_hour:account"
WEEK = "anthropic:seven_day:account"
SINCE, UNTIL = "2026-11-02T00:00:00Z", "2026-11-09T00:00:00Z"


def _seed(path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p1','P','t','t')")
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES ('steve-p1','steve','p1','A','t','t')"
        )
        db.executemany(
            "INSERT INTO vendor_accounts (id,connection_id,name,created_at,updated_at) "
            "VALUES (?,'conn',?,'t','t')",
            [("acct-a", "Seat A"), ("acct-b", "Seat B")],
        )
        db.executemany(
            "INSERT INTO limit_observations "
            "(account_id,window_key,window_kind,used_percent,resets_at,source,observed_at) "
            "VALUES (?,?,?,?,?,'rate_limit_event',?)",
            [
                # Opens the window at the limit: one hour until its reset, no stop.
                ("acct-a", FIVE, "five_hour", 100, "2026-11-02T01:00:00Z", "2026-11-01T22:00:00Z"),
                ("acct-a", FIVE, "five_hour", 10, "2026-11-02T07:00:00Z", "2026-11-02T02:00:00Z"),
                # A stop held until its reset at 12:30, before the next reading.
                ("acct-a", FIVE, "five_hour", 100, "2026-11-03T12:30:00Z", "2026-11-03T10:00:00+00:00"),
                ("acct-a", FIVE, "five_hour", 5, "2026-11-03T15:00:00Z", "2026-11-03T13:00:00Z"),
                # At the limit again after a reset: two stops, one hour then two.
                ("acct-a", FIVE, "five_hour", 100, "2026-11-04T11:00:00Z", "2026-11-04T10:00:00Z"),
                ("acct-a", FIVE, "five_hour", 100, "2026-11-04T15:00:00Z",
                 "2026-11-04T12:00:00.250000+00:00"),
                ("acct-a", FIVE, "five_hour", 20, "2026-11-04T19:00:00Z", "2026-11-04T14:00:00.250000+00:00"),
                # At the limit with no reset known and no later reading: held to the end.
                ("acct-a", FIVE, "five_hour", 100, None, "2026-11-08T22:00:00Z"),
                ("acct-a", WEEK, "seven_day", 50, "2026-11-10T00:00:00Z", "2026-11-05T00:00:00Z"),
                # No new reading inside the window; the end is exclusive.
                ("acct-b", FIVE, "five_hour", 30, None, "2026-10-30T00:00:00Z"),
                ("acct-b", FIVE, "five_hour", 100, None, "2026-11-09T00:00:00Z"),
            ],
        )


def test_limit_stops_counts_stops_and_hours_per_window(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    result = audit_reads.read_audit_metric("steve-p1", "steve", "limit_stops", since=SINCE, until=UNTIL)
    assert result["scope"] == "install_accounts"
    assert result["rows"] == [
        {"account_id": "acct-a", "account_name": "Seat A", "windows": [
            {"window_key": FIVE, "window_kind": "five_hour", "observations": 7, "stops": 4,
             "hours_at_limit": 8.5},
            {"window_key": WEEK, "window_kind": "seven_day", "observations": 1, "stops": 0,
             "hours_at_limit": 0},
        ]},
        {"account_id": "acct-b", "account_name": "Seat B", "windows": [
            {"window_key": FIVE, "window_kind": "five_hour", "observations": 0, "stops": 0,
             "hours_at_limit": 0},
        ]},
    ]
    assert "steve" not in json.dumps(result)


def test_limit_stops_window_opening_mid_stop_counts_hours_not_a_stop(isolated_db, monkeypatch):
    from infra import audit_reads

    _seed(isolated_db)
    monkeypatch.setattr(audit_reads, "DB_PATH", isolated_db)
    result = audit_reads.read_audit_metric(
        "steve-p1", "steve", "limit_stops",
        since="2026-11-03T11:00:00Z", until="2026-11-03T12:00:00Z",
    )
    five = result["rows"][0]["windows"][0]
    assert (five["observations"], five["stops"], five["hours_at_limit"]) == (0, 0, 1)


def test_limit_stops_is_listed_by_the_tool():
    from tools.audit_reads import AuditReadTool

    tool = AuditReadTool()
    assert "limit_stops" in tool.parameters["properties"]["metric"]["enum"]
    assert "limit_stops returns" in tool.description


def _work_done(history_days: int) -> int:
    """SQLite steps for one limit_stops call over a week, after `history_days` of older rows."""
    from datetime import datetime, timedelta, timezone

    from infra.audit_limit_stops import limit_stops
    from infra.limit_history_schema import LIMIT_HISTORY_SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE vendor_accounts (id TEXT PRIMARY KEY, name TEXT)")
    db.executescript(LIMIT_HISTORY_SCHEMA_SQL)
    accounts = [f"acct-{i}" for i in range(4)]
    db.executemany("INSERT INTO vendor_accounts VALUES (?,?)", [(a, a) for a in accounts])
    end = datetime(2026, 11, 9, tzinfo=timezone.utc)
    hours = range(-24 * (7 + history_days), 0)
    db.executemany(
        "INSERT INTO limit_observations "
        "(account_id,window_key,window_kind,used_percent,resets_at,source,observed_at) "
        "VALUES (?,?,'five_hour',?,?,'test',?)",
        [(a, f"k:{w}", (h * 37) % 101, (end + timedelta(hours=h + 3)).isoformat(),
          (end + timedelta(hours=h)).isoformat())
         for a in accounts for w in range(3) for h in hours],
    )
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    db.set_progress_handler(tick, 100)
    limit_stops(db, (end - timedelta(days=7)).isoformat(), end.isoformat())
    return steps[0]


def test_limit_stops_work_follows_the_week_not_the_history():
    # Index seeks only: ninety more days of older rows add almost nothing.
    week_only, with_history = _work_done(0), _work_done(90)
    assert with_history < week_only * 1.1
