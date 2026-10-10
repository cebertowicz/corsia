"""Scale proof for global cap, scope fairness, dedupe, and no starvation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from automation.continuation import admission, runner
from automation.continuation.triggers import TriggerRequest
from infra.schema import SCHEMA_SQL


class ScaleSource:
    kind = "t0_liveness"

    def __init__(self):
        self.settled = set()
        self.delivered_by_round = []

    def due(self, _now, *, db):
        requests = []
        for index in range(50):
            thread_id = f"thread-{index:02d}"
            if thread_id in self.settled:
                continue
            requests.append(
                TriggerRequest(
                    kind=self.kind,
                    thread_id=thread_id,
                    scope_id=f"scope-{index % 5}",
                    subject_id=f"node-{index:02d}",
                    transition="stable-signal",
                    reason="progress_unchanged",
                )
            )
        return requests

    def settle(self, trigger, outcome, _reason, *, db):
        if outcome == "delivered":
            self.settled.add(trigger.thread_id)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for index in range(50):
        db.execute(
            """INSERT INTO threads
               (id,user_id,created_at,updated_at)
               VALUES (?,'user','t','t')""",
            (f"thread-{index:02d}",),
        )
    db.commit()
    return db


async def _land(*_args):
    return True


@pytest.mark.asyncio
async def test_fifty_threads_across_five_scopes_are_bounded_without_starvation(
    monkeypatch,
):
    db = _db()
    source = ScaleSource()
    monkeypatch.setattr(admission, "MAX_GLOBAL_INFLIGHT", 4)
    monkeypatch.setattr(admission, "MAX_PER_SCOPE_PER_SWEEP", 2)
    monkeypatch.setattr(admission, "MAX_DEFERRALS", 20)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first_scope_round = {}

    for round_number in range(15):
        now = start + timedelta(days=30 * round_number)
        result = await runner.sweep(
            now.isoformat(),
            db=db,
            sources=[source],
            is_idle=lambda _thread_id: True,
            deliver=_land,
        )
        assert len(result["woken"]) <= 4
        for thread_id in result["woken"]:
            scope = f"scope-{int(thread_id.removeprefix('thread-')) % 5}"
            first_scope_round.setdefault(scope, round_number)
        if len(source.settled) == 50:
            break

    assert len(source.settled) == 50
    assert set(first_scope_round) == {f"scope-{index}" for index in range(5)}
    assert max(first_scope_round.values()) <= 1
    assert db.execute(
        """SELECT COUNT(*) FROM continuation_triggers
           WHERE state='delivered'"""
    ).fetchone()[0] == 50
    assert db.execute(
        """SELECT COUNT(DISTINCT dedupe_key) FROM continuation_triggers"""
    ).fetchone()[0] == 50
