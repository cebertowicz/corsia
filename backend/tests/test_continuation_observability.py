"""Content-safe continuation product events."""

from __future__ import annotations

import json
import sqlite3

import pytest

from automation.continuation import execution, runner
from automation.continuation.triggers import TriggerRequest
from infra.schema import SCHEMA_SQL


class OneRequestSource:
    kind = "t0_liveness"

    def due(self, _now, *, db):
        return [
            TriggerRequest(
                kind=self.kind,
                thread_id="thread",
                scope_id="scope",
                subject_id="node-id",
                transition="signal-hash",
                reason="progress_unchanged",
                details={
                    "title": "SECRET TITLE",
                    "description": "SECRET DESCRIPTION",
                    "acceptance_criteria": "SECRET DOD",
                },
            )
        ]

    def settle(self, *_args, **_kwargs):
        return None


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        """INSERT INTO threads
           (id,user_id,created_at,updated_at)
           VALUES ('thread','user','t','t')"""
    )
    db.commit()
    return db


@pytest.mark.asyncio
async def test_product_event_never_exposes_plan_free_text(monkeypatch):
    db = _db()
    emitted = []
    monkeypatch.setattr(
        execution,
        "emit_product_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )

    await runner.sweep(
        "2026-01-01T00:00:00+00:00",
        db=db,
        sources=[OneRequestSource()],
        deliver=lambda *_args: _land(),
    )

    delivered = db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE state='delivered'"
    ).fetchone()[0]
    assert delivered == 1  # the sweep really ran; the assertion below is not vacuous

    serialized = json.dumps({"events": emitted})
    assert "SECRET" not in serialized


async def _land():
    return True
