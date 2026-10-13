"""Continuation writes own only their synchronous unit, never an async borrower."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from automation.continuation import admission, execution, runner, triggers
from automation.continuation.sources.harvest import HarvestSource
from infra import database

NOW = "2026-08-01T00:00:00+00:00"


class Source:
    kind = "t0_liveness"

    def due(self, _now, *, db):
        return [request()]

    def settle(self, _trigger, _outcome, _reason, *, db):
        db.execute("INSERT INTO continuation_markers VALUES ('settled')")


class TransactionProbeSource(Source):
    """Record whether continuation discovery borrowed the writer."""

    def __init__(self):
        self.transaction_states = []

    def due(self, now, *, db):
        self.transaction_states.append(db.in_transaction)
        return super().due(now, db=db)


class RaisingPrepareSource:
    kind = "raising_prepare"

    def prepare(self, _now, *, db):
        db.execute("INSERT INTO continuation_markers VALUES ('failed prepare')")
        raise RuntimeError("prepare failed")

    def due(self, _now, *, db):
        return []


class RaisingDueSource:
    kind = "raising_due"

    def due(self, _now, *, db):
        db.execute("INSERT INTO continuation_markers VALUES ('failed due')")
        raise RuntimeError("due failed")


class FailCommit:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def commit(self):
        raise RuntimeError("commit rejected")


def request(**kwargs):
    return triggers.TriggerRequest(
        kind="t0_liveness", thread_id="owner", scope_id="__workspace__",
        subject_id="wake", transition="cycle:1", reason="progress_unchanged",
        **kwargs,
    )


@pytest.fixture
def continuation_db(isolated_db):
    with database.write_transaction(None, scope="continuation_test_seed") as db:
        db.execute("CREATE TABLE continuation_markers (label TEXT)")
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('owner','user','t','t')",
        )
    return isolated_db


def stored(path):
    with closing(sqlite3.connect(str(path))) as db:
        return (
            db.execute("SELECT label FROM continuation_markers ORDER BY rowid").fetchall(),
            db.execute("SELECT state,attempt FROM continuation_triggers ORDER BY id").fetchall(),
        )


def writer_available(path):
    with closing(sqlite3.connect(str(path), timeout=0.02)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def seeded_trigger(*, exhausted=False, dispatched=False):
    with database.write_transaction(None, scope="continuation_test_trigger") as db:
        trigger, _ = triggers.enqueue(request(), db=db, now=NOW)
        if exhausted:
            db.execute(
                "UPDATE continuation_triggers SET deferrals=? WHERE id=?",
                (admission.MAX_DEFERRALS, trigger.id),
            )
        if dispatched:
            triggers.claim(trigger.id, idempotency_key="recorded-input", db=db, now=NOW)
        return triggers.get(trigger.id, db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("prepare", [False, True])
async def test_sweep_cannot_finish_a_foreign_transaction(
    continuation_db, named, prepare, monkeypatch,
):
    db = database.get_db()
    db.execute("INSERT INTO continuation_markers VALUES ('caller')")
    # A named borrower shares the caller's connection and is refused its transaction. An unnamed
    # sweep runs on pool threads with their own connections, so it can never finish the caller's
    # transaction either; it meets the caller's write lock instead.
    monkeypatch.setattr(database, "CONNECTION_BUSY_TIMEOUT_MS", 50)
    refusal = database.TransactionOwnershipError if named else sqlite3.OperationalError
    try:
        with pytest.raises(refusal):
            await runner.sweep(
                NOW, db=db if named else None,
                sources=[HarvestSource()] if prepare else [Source()],
                is_idle=lambda _thread: False,
            )
        assert db.in_transaction
        assert stored(continuation_db) == ([], [])
        db.commit()
        assert stored(continuation_db) == ([("caller",)], [])
    finally:
        db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True])
async def test_delivery_cannot_settle_inside_a_later_foreign_write(
    continuation_db, named, monkeypatch,
):
    db = database.get_db()
    monkeypatch.setattr(database, "CONNECTION_BUSY_TIMEOUT_MS", 50)
    refusal = database.TransactionOwnershipError if named else sqlite3.OperationalError

    async def deliver(thread_id, _event, _summary, _details):
        assert thread_id == "owner"
        assert stored(continuation_db) == ([], [("dispatched", 1)])
        writer_available(continuation_db)
        await asyncio.sleep(0)
        db.execute("INSERT INTO continuation_markers VALUES ('later-owner')")
        return True

    try:
        with pytest.raises(refusal):
            await runner.sweep(
                NOW, db=db if named else None, sources=[Source()],
                is_idle=lambda _thread: True, deliver=deliver,
            )
        assert db.in_transaction
        assert stored(continuation_db) == ([], [("dispatched", 1)])
        db.commit()
        assert stored(continuation_db) == ([("later-owner",)], [("dispatched", 1)])
    finally:
        db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True])
async def test_sweep_commits_claim_before_delivery_and_source_with_outcome(continuation_db, named):
    async def deliver(_thread, _event, _summary, _details):
        assert stored(continuation_db) == ([], [("dispatched", 1)])
        writer_available(continuation_db)
        return True

    db = database.get_db()
    result = await runner.sweep(
        NOW, db=db if named else None, sources=[Source()],
        is_idle=lambda _thread: True, deliver=deliver,
    )
    assert result["woken"] == ["owner"]
    assert stored(continuation_db) == ([("settled",)], [("delivered", 1)])
    assert not db.in_transaction
    writer_available(continuation_db)


@pytest.mark.asyncio
async def test_sweep_skips_raising_prepare_source_and_runs_later_source(
    continuation_db, monkeypatch,
):
    db = database.get_db()
    logger = MagicMock()
    monkeypatch.setattr(runner, "logger", logger)

    result = await runner.sweep(
        NOW,
        db=db,
        sources=[RaisingPrepareSource(), Source()],
        is_idle=lambda _thread: True,
        deliver=AsyncMock(return_value=True),
    )

    assert result["woken"] == ["owner"]
    assert stored(continuation_db) == ([("settled",)], [("delivered", 1)])
    assert not db.in_transaction
    logger.exception.assert_called_once_with(
        "continuation {} source prepare failed", "raising_prepare",
    )


@pytest.mark.asyncio
async def test_sweep_skips_raising_due_source_and_runs_later_source(
    continuation_db, monkeypatch,
):
    db = database.get_db()
    logger = MagicMock()
    monkeypatch.setattr(runner, "logger", logger)

    result = await runner.sweep(
        NOW,
        db=db,
        sources=[RaisingDueSource(), Source()],
        is_idle=lambda _thread: True,
        deliver=AsyncMock(return_value=True),
    )

    assert result["woken"] == ["owner"]
    assert stored(continuation_db) == ([("settled",)], [("delivered", 1)])
    assert not db.in_transaction
    logger.exception.assert_called_once_with(
        "continuation {} source due failed", "raising_due",
    )


@pytest.mark.asyncio
async def test_collection_discovers_requests_before_acquiring_writer(continuation_db):
    source = TransactionProbeSource()

    await runner.sweep(
        NOW, sources=[source], is_idle=lambda _thread: False,
    )

    assert source.transaction_states == [False]


@pytest.mark.asyncio
async def test_collection_sql_failure_rolls_back_its_named_unit(continuation_db):
    with database.write_transaction(None, scope="continuation_test_fault") as db:
        db.execute(
            "CREATE TRIGGER reject_collection AFTER INSERT ON continuation_triggers "
            "BEGIN SELECT RAISE(ABORT, 'collection rejected'); END",
        )

    with pytest.raises(sqlite3.IntegrityError, match="collection rejected"):
        await runner.sweep(NOW, db=db, sources=[Source()])
    assert not db.in_transaction
    assert stored(continuation_db) == ([], [])
    writer_available(continuation_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["collection", "claim"])
async def test_failed_commit_releases_only_the_owned_unit(continuation_db, stage):
    db = database.get_db()
    if stage == "claim":
        trigger = seeded_trigger()
    with pytest.raises(RuntimeError, match="commit rejected"):
        if stage == "collection":
            await runner.sweep(NOW, db=FailCommit(db), sources=[Source()])
        else:
            execution.settle_without_delivery(
                trigger, request(outcome="suppressed"), Source(),
                db=FailCommit(db), now=NOW, results=defaultdict(list),
            )
    assert not db.in_transaction
    assert stored(continuation_db) == ([], [] if stage == "collection" else [("pending", 0)])
    writer_available(continuation_db)


@pytest.mark.parametrize("exhausted", [False, True])
def test_synchronous_terminalization_preserves_named_outer_rollback(continuation_db, exhausted):
    trigger = seeded_trigger(exhausted=exhausted)
    with pytest.raises(RuntimeError, match="caller abort"):
        with database.write_transaction(None, scope="continuation_test_caller") as db:
            db.execute("INSERT INTO continuation_markers VALUES ('caller')")
            if exhausted:
                execution.defer_trigger(
                    trigger, "global_in_flight", datetime.fromisoformat(NOW),
                    Source(), db=db, results=defaultdict(list),
                )
            else:
                execution.settle_without_delivery(
                    trigger, request(outcome="suppressed"), Source(),
                    db=db, now=NOW, results=defaultdict(list),
                )
            assert db.in_transaction
            assert stored(continuation_db) == ([], [("pending", 0)])
            raise RuntimeError("caller abort")
    assert stored(continuation_db) == ([], [("pending", 0)])
    writer_available(continuation_db)


@pytest.mark.parametrize("outcome", ["suppressed", "stalled", "deferred"])
def test_standalone_terminal_and_deferred_units_are_durable(continuation_db, outcome):
    trigger = seeded_trigger(exhausted=outcome == "stalled")
    results = defaultdict(list)
    if outcome == "suppressed":
        execution.settle_without_delivery(
            trigger, request(outcome=outcome), Source(), db=None, now=NOW, results=results,
        )
    else:
        execution.defer_trigger(
            trigger, "global_in_flight", datetime.fromisoformat(NOW),
            Source(), db=None, results=results,
        )
    assert stored(continuation_db) == (
        [("settled",)], [(outcome, 0 if outcome == "deferred" else 1)],
    )
    writer_available(continuation_db)


def test_a_busy_owner_retries_at_the_base_interval_without_spending_the_budget(continuation_db):
    """#505: a running Turn is not a capacity limit, so it never stalls the wake."""
    trigger = seeded_trigger(exhausted=True)
    results = defaultdict(list)
    now = datetime.fromisoformat(NOW)

    execution.defer_trigger(
        trigger, "thread_unavailable", now, Source(), db=None, results=results,
    )

    after = current(trigger.id)
    assert (after.state, after.reason, after.deferrals) == (
        "deferred", "thread_unavailable", admission.MAX_DEFERRALS,
    )
    delay = (datetime.fromisoformat(after.next_attempt_at) - now).total_seconds()
    base = admission.DEFERRAL_BASE_SECONDS
    assert base * (1 - admission.JITTER_RATIO) <= delay <= base * (1 + admission.JITTER_RATIO)
    assert results["stalled"] == [] and results["skipped"] == ["owner"]


@pytest.mark.parametrize("reason", ["global_in_flight", "scope_cost_ceiling", "scope_fairness"])
def test_a_capacity_deferral_at_the_budget_still_stalls(continuation_db, reason):
    trigger = seeded_trigger(exhausted=True)
    results = defaultdict(list)

    execution.defer_trigger(
        trigger, reason, datetime.fromisoformat(NOW), Source(), db=None, results=results,
    )

    assert current(trigger.id).state == "stalled"
    assert results["stalled"] == ["owner"]


def test_a_capacity_deferral_keeps_its_exponential_backoff_and_count(continuation_db):
    trigger = seeded_trigger()
    now = datetime.fromisoformat(NOW)

    execution.defer_trigger(
        trigger, "global_in_flight", now, Source(), db=None, results=defaultdict(list),
    )

    after = current(trigger.id)
    assert (after.state, after.deferrals) == ("deferred", 1)
    assert after.next_attempt_at == admission.deferred_until(trigger.id, 1, now).isoformat()


@pytest.mark.asyncio
async def test_a_trigger_outlasts_a_busy_owner_and_is_delivered_once_it_is_idle(continuation_db):
    """#505: an owner busy for more sweeps than the budget still gets its wake."""
    delivered = []

    async def deliver(thread, event, summary, details):
        delivered.append(thread)
        return True

    start = datetime.fromisoformat(NOW)
    busy_sweeps = admission.MAX_DEFERRALS * 3
    for index in range(busy_sweeps):
        result = await runner.sweep(
            (start + timedelta(minutes=5 * index)).isoformat(), sources=[Source()],
            is_idle=lambda _thread: False, deliver=deliver,
        )
        assert result["stalled"] == []
    [row] = [current(trigger_id) for trigger_id in _trigger_ids(continuation_db)]
    assert (row.state, row.deferrals, delivered) == ("deferred", 0, [])

    result = await runner.sweep(
        (start + timedelta(minutes=5 * busy_sweeps)).isoformat(), sources=[Source()],
        is_idle=lambda _thread: True, deliver=deliver,
    )

    assert result["woken"] == ["owner"] and delivered == ["owner"]
    assert current(row.id).state == "delivered"


def current(trigger_id):
    return triggers.get(trigger_id, db=database.get_db())


def _trigger_ids(path):
    with closing(sqlite3.connect(str(path))) as db:
        return [row[0] for row in db.execute("SELECT id FROM continuation_triggers")]


@pytest.mark.asyncio
async def test_failed_accepted_delivery_wins_race_with_inflight_settlement(
    continuation_db, monkeypatch,
):
    trigger = seeded_trigger()
    results = defaultdict(list)
    events = []
    monkeypatch.setattr(
        execution, "emit_product_event",
        lambda category, action, *, properties: events.append(
            (category, action, properties["corrects"]),
        ),
    )

    async def deliver(*_args):
        with database.write_transaction(None, scope="test_failed_accepted_delivery") as db:
            claimed = triggers.get(trigger.id, db=db)
            assert claimed.state == "dispatched"
            execution.record_failed_accepted_delivery(
                claimed.idempotency_key, db=db, now=NOW,
            )
        return True

    await execution.deliver_one(
        trigger, request(), Source(), deliver, db=None, now=NOW, results=results,
    )

    assert stored(continuation_db) == ([], [("failed", 1)])
    assert results == {}
    assert events == [("continuation", "failed", "dispatched")]


@pytest.mark.asyncio
async def test_direct_delivery_rejects_an_active_explicit_owner(continuation_db):
    trigger = seeded_trigger()
    db = database.get_db()
    db.execute("INSERT INTO continuation_markers VALUES ('caller')")

    async def deliver(*_args):
        pytest.fail("delivery ran before the caller transaction ended")

    try:
        with pytest.raises(database.TransactionOwnershipError):
            await execution.deliver_one(
                trigger, request(), Source(), deliver, db=db, now=NOW,
                results=defaultdict(list),
            )
        assert db.in_transaction
        assert stored(continuation_db) == ([], [("pending", 0)])
        db.commit()
        assert stored(continuation_db) == ([("caller",)], [("pending", 0)])
    finally:
        db.rollback()
