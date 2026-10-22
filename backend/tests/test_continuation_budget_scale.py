"""Scale proof: every due wake lands in one sweep, and dedupe holds."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from automation.continuation import runner
from automation.continuation.triggers import TriggerRequest
from infra.schema import SCHEMA_SQL


class ScaleSource:
    kind = "t0_liveness"

    def __init__(self):
        self.settled = set()
        self.delivered_by_round = []

    def due(self, _now, *, db):
        requests = []
        for index in range(20):
            thread_id = f"thread-{index:02d}"
            if thread_id in self.settled:
                continue
            requests.append(
                TriggerRequest(
                    kind=self.kind,
                    thread_id=thread_id,
                    scope_id="scope-0" if index < 10 else f"scope-{index}",
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
    for index in range(20):
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
async def test_ten_waits_for_one_scope_and_ten_for_others_all_land_in_one_sweep():
    db = _db()
    source = ScaleSource()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()

    result = await runner.sweep(now, db=db, sources=[source], deliver=_land)

    assert len(result["woken"]) == 20
    assert len(source.settled) == 20
    assert dict(
        db.execute(
            "SELECT state, COUNT(*) FROM continuation_triggers GROUP BY state"
        ).fetchall()
    ) == {"delivered": 20}

    again = await runner.sweep(now, db=db, sources=[source], deliver=_land)
    assert again["woken"] == []
