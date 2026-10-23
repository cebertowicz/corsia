"""A due sweep commits and re-checks each batch on its own, on a real SQLite file."""

from __future__ import annotations

import sqlite3

import pytest

import infra.database as database
from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import TransactionOwnershipError, get_db
from infra.db_async import run_bulk, run_read
from infra.db_connection import LaneWriteError

PAST = "2000-01-01T00:00:00+00:00"


def _two_due_batches(db) -> tuple[str, str]:
    seed_thread(db, "owner-a")
    seed_thread(db, "owner-b")
    seed_thread(db, "author")
    for owner in ("owner-a", "owner-b"):
        add_comment(db, seed_document(db, owner, name=f"{owner}.md"))
    db.execute("UPDATE doc_feedback_batches SET closes_at=?", (PAST,))
    db.commit()
    first, second = (row["id"] for row in db.execute("SELECT id FROM doc_feedback_batches ORDER BY closes_at,id"))
    return first, second


def _state(db, batch_id: str) -> tuple[str, str | None]:
    row = db.execute("SELECT state,delivered_node_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()
    return row["state"], row["delivered_node_id"]


def test_each_due_batch_is_visible_as_committed_before_the_next_claim_starts(isolated_db, monkeypatch):
    db = get_db()
    try:
        first, second = _two_due_batches(db)
        seen: dict[str, str] = {}
        original = feedback_batches._claim_and_deliver

        def spy(connection, batch_id, **kwargs):
            if batch_id == second:
                other = sqlite3.connect(str(database.DB_PATH))
                try:
                    seen["first"] = other.execute(
                        "SELECT state FROM doc_feedback_batches WHERE id=?", (first,),
                    ).fetchone()[0]
                finally:
                    other.close()
            return original(connection, batch_id, **kwargs)

        monkeypatch.setattr(feedback_batches, "_claim_and_deliver", spy)

        feedback_batches.close_due(PAST, db=db)

        assert seen == {"first": "delivered"}
        assert [_state(db, first)[0], _state(db, second)[0]] == ["delivered", "delivered"]
    finally:
        db.close()


def test_batch_touched_after_selection_stays_open_and_undelivered(isolated_db, monkeypatch):
    db = get_db()
    try:
        first, second = _two_due_batches(db)
        original = feedback_batches._close_due_batch

        def touch_then_close(connection, batch_id, now):
            if batch_id == first:
                feedback_batches._touch_batch(
                    connection, first, author_kind="agent", now=feedback_batches._now(), item_delta=1,
                )
                connection.commit()
            return original(connection, batch_id, now)

        monkeypatch.setattr(feedback_batches, "_close_due_batch", touch_then_close)

        woken = feedback_batches.close_due(PAST, db=db)

        assert _state(db, first) == ("open", None)
        assert _state(db, second)[0] == "delivered"
        second_recipient = db.execute(
            "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (second,),
        ).fetchone()["recipient_id"]
        assert woken == [second_recipient]
    finally:
        db.close()


def test_due_sweep_refuses_a_pooled_connection_that_is_mid_transaction(isolated_db):
    db = get_db()
    try:
        first, second = _two_due_batches(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE doc_feedback_batches SET item_count=item_count+7")

        with pytest.raises(TransactionOwnershipError):
            feedback_batches.close_due(PAST)

        assert db.in_transaction
        assert db.execute("SELECT SUM(item_count) FROM doc_feedback_batches").fetchone()[0] >= 14
        db.rollback()
        assert [_state(db, first)[0], _state(db, second)[0]] == ["open", "open"]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("run_on_lane", [run_read, run_bulk], ids=["read", "bulk"])
async def test_due_sweep_on_a_read_or_bulk_lane_fails_once_before_claiming_any_batch(isolated_db, monkeypatch, run_on_lane):
    db = get_db()
    try:
        first, second = _two_due_batches(db)
        claims: list[str] = []
        original = feedback_batches._claim_and_deliver

        def spy(connection, batch_id, **kwargs):
            claims.append(batch_id)
            return original(connection, batch_id, **kwargs)

        monkeypatch.setattr(feedback_batches, "_claim_and_deliver", spy)

        with pytest.raises(LaneWriteError):
            await run_on_lane(feedback_batches.close_due, PAST)

        assert claims == []
        assert [_state(db, first)[0], _state(db, second)[0]] == ["open", "open"]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("from_state", "reason", "due_by"),
    [("open", "quiet", PAST), ("open", None, None), ("undelivered", "quiet", None), ("undelivered", None, PAST)],
)
def test_claim_refuses_a_reason_and_cutoff_combination_that_is_not_one_route(isolated_db, from_state, reason, due_by):
    db = get_db()
    try:
        with pytest.raises(ValueError):
            feedback_batches._claim_and_deliver(db, "batch", from_state=from_state, reason=reason, due_by=due_by)
    finally:
        db.close()
