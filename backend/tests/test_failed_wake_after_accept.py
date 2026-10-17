"""A wake accepted into a thread's FIFO that later ends ``failed`` is reported (#2521).

Routing reports a wake delivered once the FIFO accepts it; admission to a Turn
happens later and can refuse (an unavailable pinned model). The settlement that
fails the row writes the report (or a dead-letter row) in the same transaction;
the owning frame then drives the owner best-effort after the commit.
"""

from __future__ import annotations

import json

import pytest

from events import delivery
from infra import failed_wake_surface as surface
from infra.database import get_db, write_transaction
from infra.failed_wake_surface import REPORT_EVENT, WAKE_FAILED_REASON, drive_failed_wake_owners
from infra.input_admission import accept_thread_input
from infra.input_sequencing import sequence_attempt
from infra.queued_input_settlement import fail_queued
from infra.thread_input_types import FailedInputBatch
from tests.event_routing_helpers import mk_session

ADMISSION_ERROR = "external checkpoint boundary changed before admission"


def _accept_wake(thread: str, message_id: str) -> None:
    delivery.enqueue_event_wake(
        thread, "webhook.github.check_suite_completed", "CI finished", {"url": "u"},
        message_id=message_id,
    )


def _parent(child: str, parent: str) -> None:
    with write_transaction(None, scope="test_parent") as db:
        db.execute("UPDATE threads SET parent_thread_id=? WHERE id=?", (parent, child))


def _queued(message_id: str):
    return get_db().execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchall()


def _fail_via_attempt(message_id: str, state: str = "failed") -> tuple[str, ...]:
    with write_transaction(None, scope="test_end_wake") as db:
        return sequence_attempt(
            db, _queued(message_id), state=state,
            outcome="accepted" if state == "delivered" else "proved_no_write",
            error=ADMISSION_ERROR if state == "failed" else None,
        )


def _reports(thread: str) -> list:
    return get_db().execute(
        "SELECT id,content FROM messages WHERE thread_id=? AND metadata LIKE ? ORDER BY id",
        (thread, f"%{REPORT_EVENT}%"),
    ).fetchall()


def _parked() -> list[dict]:
    return [
        json.loads(row["payload_json"]) for row in get_db().execute(
            "SELECT payload_json FROM dead_letter_events WHERE reason=?", (WAKE_FAILED_REASON,),
        ).fetchall()
    ]


def test_a_wake_that_fails_after_accept_is_reported_in_the_same_commit(isolated_db):
    child, owner = mk_session("wake-target"), mk_session("wake-owner")
    _parent(child, owner)
    _accept_wake(child, "m-failed")

    failed = _fail_via_attempt("m-failed")  # no drive ever runs: the row is already durable

    [report] = _reports(owner)
    assert failed == ("m-failed",)
    assert ADMISSION_ERROR in report["content"] and child in report["content"]
    assert _reports(child) == [] and _parked() == []


def test_a_queued_wake_failed_in_place_is_reported(isolated_db):
    child, owner = mk_session("queued-target"), mk_session("queued-owner")
    _parent(child, owner)
    _accept_wake(child, "m-queued")

    with write_transaction(None, scope="test_fail_queued") as db:
        fail_queued(db, child, _queued("m-queued"), error=ADMISSION_ERROR)

    assert len(_reports(owner)) == 1


def test_no_live_owner_or_a_self_parented_thread_parks_the_failure(isolated_db):
    orphan, self_parented = mk_session("orphan-target"), mk_session("self-parented")
    _parent(self_parented, self_parented)
    _accept_wake(orphan, "m-orphan")
    _accept_wake(self_parented, "m-self")

    _fail_via_attempt("m-orphan")
    _fail_via_attempt("m-self")

    assert _reports(orphan) == [] and _reports(self_parented) == []
    assert sorted(p["message_id"] for p in _parked()) == ["m-orphan", "m-self"]
    assert all(p["delivery_error"] == ADMISSION_ERROR for p in _parked())


def test_an_enqueue_the_owner_refuses_parks_and_the_settlement_still_commits(isolated_db, monkeypatch):
    child, owner = mk_session("refused-target"), mk_session("refused-owner")
    _parent(child, owner)
    _accept_wake(child, "m-refused")

    def refuse(*_a, **_kw):
        raise ValueError("event wake target is unavailable")

    monkeypatch.setattr(delivery, "enqueue_event_wake", refuse)
    _fail_via_attempt("m-refused")

    assert _reports(owner) == []
    assert [p["message_id"] for p in _parked()] == ["m-refused"]
    assert _queued("m-refused")[0]["delivery_state"] == "failed"


def test_a_retried_wake_that_fails_again_reaches_the_owner_again(isolated_db):
    child, owner = mk_session("retry-target"), mk_session("retry-owner")
    _parent(child, owner)
    _accept_wake(child, "m-retry")
    _fail_via_attempt("m-retry")

    with write_transaction(None, scope="test_retry") as db:
        db.execute(
            "UPDATE messages SET delivery_state='queued',write_outcome='proved_no_write',"
            "delivery_error=NULL,delivery_revision=delivery_revision+1 WHERE id='m-retry'",
        )
    with write_transaction(None, scope="test_fail_again") as db:
        fail_queued(db, child, _queued("m-retry"), error=ADMISSION_ERROR)

    assert len(_reports(owner)) == 2


def test_a_failed_report_goes_to_its_own_owner_and_the_chain_ends_in_a_park(isolated_db):
    child, owner, grandparent = (mk_session(n) for n in ("c", "o", "g"))
    _parent(child, owner)
    _parent(owner, grandparent)
    _accept_wake(child, "m-first")
    _fail_via_attempt("m-first")
    [report] = _reports(owner)

    _fail_via_attempt(report["id"])
    [second] = _reports(grandparent)
    assert _parked() == []

    _fail_via_attempt(second["id"])  # the grandparent has no owner: the chain ends
    assert [p["thread_id"] for p in _parked()] == [grandparent]


def test_a_cycle_of_owners_parks_instead_of_looping(isolated_db):
    a, b = mk_session("cycle-a"), mk_session("cycle-b")
    _parent(a, b)
    _parent(b, a)
    _accept_wake(a, "m-cycle")
    _fail_via_attempt("m-cycle")
    [report] = _reports(b)

    _fail_via_attempt(report["id"])

    assert _reports(a) == [] and [p["thread_id"] for p in _parked()] == [b]


def test_a_delivered_wake_and_a_non_wake_failure_report_nothing(isolated_db):
    child, owner = mk_session("quiet-target"), mk_session("quiet-owner")
    _parent(child, owner)
    _accept_wake(child, "m-ok")
    _fail_via_attempt("m-ok", "delivered")
    accept_thread_input(
        child, role="user", content="hi", intent="apply_now", message_id="m-user",
        metadata={}, delivery_options={"native_mode": "event", "attachments": []},
    )
    _fail_via_attempt("m-user")

    assert _reports(owner) == [] and _parked() == []


@pytest.mark.asyncio
async def test_the_owner_is_driven_and_a_drive_failure_never_raises(isolated_db, monkeypatch):
    import infra.thread_execution as execution

    child, owner = mk_session("drive-target"), mk_session("drive-owner")
    _parent(child, owner)
    _accept_wake(child, "m-drive")
    failed = _fail_via_attempt("m-drive")
    driven: list[str] = []

    async def spy(thread_id):
        driven.append(thread_id)

    monkeypatch.setattr(execution, "drive_thread", spy)
    await drive_failed_wake_owners(failed)
    assert driven == [owner]

    async def broken(_thread_id):
        raise RuntimeError("drive exploded")

    monkeypatch.setattr(execution, "drive_thread", broken)
    await drive_failed_wake_owners(failed)  # must not raise


@pytest.mark.asyncio
async def test_the_admission_driver_drives_the_owner_after_its_existing_drives(isolated_db, monkeypatch):
    import infra.thread_execution as execution
    import infra.turn_admission_driver as driver

    order: list[str] = []

    async def broadcast(*_a, **_kw):
        order.append("broadcast")

    async def switch(*_a, **_kw):
        order.append("switch")

    async def drive(thread_id):
        order.append(f"drive:{thread_id}")

    monkeypatch.setattr(driver, "broadcast_message_rows", broadcast)
    monkeypatch.setattr("thread.model_pending.drive_pending_model_switch", switch)
    monkeypatch.setattr(execution, "drive_thread", drive)
    child, owner = mk_session("driver-target"), mk_session("driver-owner")
    _parent(child, owner)
    _accept_wake(child, "m-driven")
    failed = _fail_via_attempt("m-driven")

    await driver.drive_turn_admission(FailedInputBatch(child, failed, ADMISSION_ERROR))

    assert order == ["broadcast", "switch", f"drive:{owner}"]
