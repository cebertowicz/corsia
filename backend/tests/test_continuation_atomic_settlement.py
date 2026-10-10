"""Atomic rollback tests for non-delivery continuation settlement paths."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from automation.continuation import admission, execution, triggers
from infra.schema import SCHEMA_SQL


class _CrashingSource:
    kind = "t0_liveness"

    def settle(self, _trigger, _outcome, _reason, *, db) -> None:
        db.execute("INSERT INTO settlement_markers DEFAULT VALUES")
        raise RuntimeError("source settlement failed")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute("CREATE TABLE settlement_markers (id INTEGER PRIMARY KEY)")
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('thread','user','t','t')"
    )
    db.commit()
    return db


def _request(*, outcome=None, reason=None) -> triggers.TriggerRequest:
    return triggers.TriggerRequest(
        kind="t0_liveness",
        thread_id="thread",
        scope_id="__workspace__",
        subject_id="wake",
        transition="cycle:1",
        outcome=outcome,
        reason=reason,
    )


def _trigger(db, *, outcome=None, reason=None):
    trigger, _created = triggers.enqueue(
        _request(outcome=outcome, reason=reason),
        db=db,
        now="2026-01-01T00:00:00+00:00",
    )
    db.commit()
    return trigger


def _results():
    return {
        key: []
        for key in ("woken", "stalled", "cancelled", "errored", "skipped")
    }


def test_terminal_without_delivery_rolls_back_trigger_and_source_together():
    db = _db()
    trigger = _trigger(db, outcome="suppressed", reason="no_actionable_work")

    with pytest.raises(RuntimeError, match="source settlement failed"):
        execution.settle_without_delivery(
            trigger,
            _request(outcome="suppressed", reason="no_actionable_work"),
            _CrashingSource(),
            db=db,
            now="2026-01-01T00:00:01+00:00",
            results=_results(),
        )

    assert triggers.get(trigger.id, db=db).state == "dispatched"
    assert db.execute("SELECT COUNT(*) FROM settlement_markers").fetchone()[0] == 0


def test_deferral_exhaustion_rolls_back_trigger_and_source_together():
    db = _db()
    trigger = _trigger(db)
    db.execute(
        "UPDATE continuation_triggers SET deferrals=? WHERE id=?",
        (admission.MAX_DEFERRALS, trigger.id),
    )
    db.commit()
    exhausted = triggers.get(trigger.id, db=db)

    with pytest.raises(RuntimeError, match="source settlement failed"):
        execution.defer_trigger(
            exhausted,
            "global_in_flight",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            _CrashingSource(),
            db=db,
            results=_results(),
        )

    stored = triggers.get(trigger.id, db=db)
    assert stored.state == "dispatched"
    assert stored.deferrals == admission.MAX_DEFERRALS
    assert db.execute("SELECT COUNT(*) FROM settlement_markers").fetchone()[0] == 0
