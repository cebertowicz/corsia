"""One-journal passive context delivery through the existing message FIFO."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t0','t0')"
    )
    for thread_id in ("thread", "other"):
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,project_id,model,created_at,updated_at) "
            "VALUES (?, 'user', 'project', 'native/model', 't0', 't0')",
            (thread_id,),
        )
    for artifact_id in ("artifact", "unrelated"):
        db.execute(
            "INSERT INTO artifact_identities "
            "(id,integration,type,external_id,state,retention,created_at,updated_at) "
            "VALUES (?,'github','pr',?,'active','durable','t0','t0')",
            (artifact_id, artifact_id),
        )
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at) "
        "VALUES ('registration','artifact','thread','subscriber-recent','t0','t0')"
    )
    return db


def test_materializes_one_coalesced_digest_and_advances_past_irrelevant_changes(
    monkeypatch,
):
    from infra.passive_changes import append_change, materialize_pending_digest

    db = _db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        append_change(
            db,
            object_kind="artifact",
            object_id="artifact",
            change_kind="updated",
            summary="old artifact state",
            created_at="t1",
        )
        append_change(
            db,
            object_kind="artifact",
            object_id="unrelated",
            change_kind="updated",
            summary="not subscribed",
            created_at="t2",
        )
        append_change(
            db,
            object_kind="artifact",
            object_id="artifact",
            change_kind="updated",
            summary="current artifact state",
            created_at="t3",
        )

        accepted = materialize_pending_digest(db, "thread")

        assert accepted is not None
        row = db.execute(
            "SELECT role,content,metadata,delivery_state,input_order,seq "
            "FROM messages WHERE id=?",
            (accepted.message_id,),
        ).fetchone()
        metadata = json.loads(row["metadata"])
        assert (row["role"], row["delivery_state"], row["input_order"], row["seq"]) == (
            "system", "queued", 1, None,
        )
        assert metadata["type"] == "context_event"
        assert metadata["event_type"] == "context.digest"
        assert metadata["event_disposition"] == "passive"
        assert [event["summary"] for event in metadata["context_events"]] == [
            "current artifact state"
        ]
        assert "current artifact state" in row["content"]
        assert db.execute(
            "SELECT last_context_change_seq FROM threads WHERE id='thread'"
        ).fetchone()[0] == 3
        assert materialize_pending_digest(db, "thread") is None
    finally:
        db.close()


def test_suppresses_self_authored_change_but_advances_the_cursor(monkeypatch):
    from infra.passive_changes import append_change, materialize_pending_digest

    db = _db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        append_change(
            db,
            object_kind="artifact",
            object_id="artifact",
            change_kind="updated",
            summary="already known",
            actor_thread_id="thread",
            created_at="t1",
        )

        assert materialize_pending_digest(db, "thread") is None
        assert db.execute(
            "SELECT last_context_change_seq FROM threads WHERE id='thread'"
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        db.close()


def test_next_independent_input_claims_digest_before_user_message(monkeypatch):
    from infra.passive_changes import append_change
    from infra.thread_driver import admit_input

    db = _db()
    try:
        monkeypatch.setattr(
            "infra.passive_changes.utc_now_iso",
            lambda: "2026-09-02T19:00:00",
        )
        append_change(
            db,
            object_kind="artifact",
            object_id="artifact",
            change_kind="updated",
            summary="review changed",
            created_at="t1",
        )
        db.commit()
        admission = admit_input(
            "thread",
            role="user",
            content="continue",
            intent="apply_now",
            message_id="user-input",
            turn_id="turn-1",
            boot_epoch="boot-1",
            admitted_at="t2",
            db=db,
        )

        assert admission.passive is not None
        assert admission.batch is not None
        assert [item.message_id for item in admission.batch.items] == [
            admission.passive.message_id,
            "user-input",
        ]
    finally:
        db.close()


def test_event_wake_queues_digest_before_the_wake(monkeypatch):
    from events.delivery import enqueue_event_wake
    from infra.passive_changes import append_change

    db = _db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        append_change(
            db,
            object_kind="artifact",
            object_id="artifact",
            change_kind="updated",
            summary="artifact changed",
            created_at="t1",
        )

        receipt = enqueue_event_wake(
            "thread",
            "review.requested",
            "Review requested",
            message_id="wake",
            created_at="t2",
            db=db,
        )

        rows = db.execute(
            "SELECT id,metadata FROM messages ORDER BY input_order"
        ).fetchall()
        assert receipt.message_id == "wake"
        assert [row["id"] for row in rows] == [
            "context-digest:thread:1:1",
            "wake",
        ]
        assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
        assert json.loads(rows[1]["metadata"])["event_disposition"] == "wake"
    finally:
        db.close()


def test_digest_detail_is_bounded_and_cursor_still_advances(monkeypatch):
    from infra.passive_changes import append_change, materialize_pending_digest

    db = _db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        for index in range(102):
            append_change(
                db,
                object_kind="global_context",
                object_id=f"global-{index}",
                change_kind="updated",
                summary=f"context change {index}",
                created_at=f"t{index:03d}",
            )

        accepted = materialize_pending_digest(db, "thread")

        assert accepted is not None
        row = db.execute(
            "SELECT metadata FROM messages WHERE id=?", (accepted.message_id,)
        ).fetchone()
        metadata = json.loads(row["metadata"])
        assert metadata["detail_truncated"] is True
        assert len(metadata["context_events"]) == 100
        assert db.execute(
            "SELECT last_context_change_seq FROM threads WHERE id='thread'"
        ).fetchone()[0] == 102
    finally:
        db.close()


def _storage_at(local: datetime) -> str:
    return local.astimezone(UTC).replace(tzinfo=None).isoformat()


def _local_at(hour: int, minute: int = 0, *, day: int = 2) -> datetime:
    local_tz = datetime.now().astimezone().tzinfo
    return datetime(2026, 9, day, hour, minute, tzinfo=local_tz)


def test_clock_visibility_is_sparse_and_uses_the_existing_passive_message():
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    try:
        start = _local_at(12)
        start_at = _storage_at(start)
        with patch("infra.passive_changes.utc_now_iso", return_value=start_at):
            first = materialize_pending_digest(db, "thread")
        assert first is not None
        first_row = db.execute(
            "SELECT content,metadata FROM messages WHERE id=?", (first.message_id,)
        ).fetchone()
        first_meta = json.loads(first_row["metadata"])
        event = first_meta["context_events"][0]
        assert event["event_type"] == "context.clock"
        assert event["summary"] == f"Current time: {start:%Y-%m-%d %H:%M %Z}"
        clock_at = datetime.fromisoformat(first_meta["context_clock_at"])
        assert clock_at.tzinfo is not None
        assert first_row["content"].count(event["summary"]) == 1

        before_due = _storage_at(start + timedelta(hours=2, seconds=-1))
        with patch("infra.passive_changes.utc_now_iso", return_value=before_due):
            assert materialize_pending_digest(db, "thread") is None
        due = _storage_at(start + timedelta(hours=2))
        with patch("infra.passive_changes.utc_now_iso", return_value=due):
            refreshed = materialize_pending_digest(db, "thread")
        assert refreshed is not None
    finally:
        db.close()


def test_clock_visibility_refreshes_on_date_change_before_two_hours():
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    try:
        before_midnight = _local_at(23, 30)
        after_midnight = _local_at(0, 15, day=3)
        with patch(
            "infra.passive_changes.utc_now_iso",
            return_value=_storage_at(before_midnight),
        ):
            assert materialize_pending_digest(db, "thread") is not None
        with patch(
            "infra.passive_changes.utc_now_iso",
            return_value=_storage_at(after_midnight),
        ):
            refreshed = materialize_pending_digest(db, "thread")
        assert refreshed is not None
    finally:
        db.close()


@pytest.mark.parametrize("state", ["queued", "delivered", "ambiguous"])
def test_model_bound_clock_suppresses_duplicate_visibility(state):
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    try:
        start = _local_at(12)
        with patch(
            "infra.passive_changes.utc_now_iso", return_value=_storage_at(start),
        ):
            first = materialize_pending_digest(db, "thread")
        assert first is not None
        db.execute(
            "UPDATE messages SET delivery_state=? WHERE id=?",
            (state, first.message_id),
        )
        with patch(
            "infra.passive_changes.utc_now_iso",
            return_value=_storage_at(start + timedelta(minutes=5)),
        ):
            assert materialize_pending_digest(db, "thread") is None
    finally:
        db.close()


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_unwritten_clock_allows_fresh_visibility(state):
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    try:
        start = _local_at(12)
        with patch(
            "infra.passive_changes.utc_now_iso", return_value=_storage_at(start),
        ):
            first = materialize_pending_digest(db, "thread")
        assert first is not None
        db.execute(
            "UPDATE messages SET delivery_state=? WHERE id=?",
            (state, first.message_id),
        )
        with patch(
            "infra.passive_changes.utc_now_iso",
            return_value=_storage_at(start + timedelta(minutes=5)),
        ):
            assert materialize_pending_digest(db, "thread") is not None
    finally:
        db.close()


def test_clock_coalesces_with_overflow_without_becoming_an_object_kind():
    from infra.passive_changes import append_change, materialize_pending_digest

    db = _db()
    try:
        for index in range(102):
            append_change(
                db,
                object_kind="global_context",
                object_id=f"global-{index}",
                change_kind="updated",
                summary=f"context change {index}",
                created_at=f"t{index:03d}",
            )
        with patch(
            "infra.passive_changes.utc_now_iso",
            return_value=_storage_at(_local_at(12)),
        ):
            accepted = materialize_pending_digest(db, "thread")
        assert accepted is not None
        row = db.execute(
            "SELECT metadata FROM messages WHERE id=?", (accepted.message_id,),
        ).fetchone()
        metadata = json.loads(row["metadata"])
        assert metadata["detail_truncated"] is True
        assert len(metadata["context_events"]) == 101
        assert metadata["context_events"][-1]["event_type"] == "context.clock"
    finally:
        db.close()


def test_clock_returns_after_a_checkpoint_folds_its_stamped_row_out_of_view():
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    try:
        start = _local_at(9)
        with patch(
            "infra.passive_changes.utc_now_iso", return_value=_storage_at(start),
        ):
            first = materialize_pending_digest(db, "thread")
        assert first is not None
        db.execute(
            "UPDATE messages SET delivery_state='delivered', seq=1 WHERE id=?",
            (first.message_id,),
        )
        db.execute(
            "INSERT INTO checkpoints "
            "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
            "status,source_harness,source_model,writer_harness,writer_model,"
            "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
            "started_at,captured_at,installed_at,ended_at) "
            "SELECT 'cp','thread',history_epoch,1,?,'pressure','warm','installed',"
            "'native','m','native','m',0,'[]','r','t','t','t','t' "
            "FROM threads WHERE id='thread'",
            (first.message_id,),
        )
        with patch(
            "infra.passive_changes.utc_now_iso",
            return_value=_storage_at(start + timedelta(minutes=30)),
        ):
            refreshed = materialize_pending_digest(db, "thread")
        assert refreshed is not None
        assert "Current time:" in db.execute(
            "SELECT content FROM messages WHERE id=?", (refreshed.message_id,),
        ).fetchone()["content"]
    finally:
        db.close()
