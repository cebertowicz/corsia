"""Durable continuation trigger repository contracts."""

from __future__ import annotations

import sqlite3

from automation.continuation import triggers
from infra.schema import SCHEMA_SQL


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('thread','user','t','t')"
    )
    db.commit()
    return db


def _request() -> triggers.TriggerRequest:
    return triggers.TriggerRequest(
        kind="t0_liveness",
        thread_id="thread",
        scope_id="__workspace__",
        subject_id="wake",
        transition="cycle:1",
        cost_estimate_usd=0.25,
    )


def test_live_dedupe_is_a_partial_unique_database_invariant() -> None:
    db = _db()
    first, created = triggers.enqueue(
        _request(), db=db, now="2026-01-01T00:00:00+00:00",
    )
    replay, replay_created = triggers.enqueue(
        _request(), db=db, now="2026-01-01T00:00:01+00:00",
    )

    assert created is True
    assert replay_created is False
    assert replay.id == first.id
    assert db.execute(
        "SELECT COUNT(*) FROM continuation_triggers"
    ).fetchone()[0] == 1


def test_claim_and_terminal_settlement_happen_exactly_once() -> None:
    db = _db()
    trigger, _ = triggers.enqueue(
        _request(), db=db, now="2026-01-01T00:00:00+00:00",
    )

    assert triggers.claim(
        trigger.id,
        idempotency_key="event:key",
        db=db,
        now="2026-01-01T00:00:01+00:00",
    )
    assert not triggers.claim(
        trigger.id,
        idempotency_key="event:key",
        db=db,
        now="2026-01-01T00:00:02+00:00",
    )
    assert triggers.settle(
        trigger.id,
        outcome="delivered",
        reason="progress_unchanged",
        db=db,
        now="2026-01-01T00:00:03+00:00",
    )
    assert not triggers.settle(
        trigger.id,
        outcome="delivered",
        reason="progress_unchanged",
        db=db,
        now="2026-01-01T00:00:04+00:00",
    )

    stored = triggers.get(trigger.id, db=db)
    assert stored.state == "delivered"
    assert stored.reason == "progress_unchanged"
    assert stored.attempt == 1
