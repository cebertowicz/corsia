"""Recovery and preparation preserve ownership before and after callbacks."""

from contextlib import closing
import asyncio
import sqlite3

import pytest

from automation.continuation import recovery, runner
from automation.continuation.sources.harvest import HarvestSource
from infra import database
from plans import service
from tests.test_continuation_transactions import (
    NOW, FailCommit, Source, continuation_db, seeded_trigger, stored, writer_available,
)
from tests.test_plan_prepare_notifications import _seed_attachment


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("named", [False, True])
async def test_recovery_callback_is_outside_the_write(continuation_db, recorded, named):
    seeded_trigger(dispatched=True)

    async def find_input(thread_id, key):
        assert (thread_id, key) == ("owner", "recorded-input")
        assert stored(continuation_db) == ([], [("dispatched", 1)])
        writer_available(continuation_db)
        return {"id": key} if recorded else None

    result = await recovery.recover_dispatched(
        db=database.get_db() if named else None, find_input=find_input,
        sources=[Source()], now=NOW,
    )
    assert result == {"delivered": int(recorded), "retried": int(not recorded)}
    assert stored(continuation_db) == (
        [("settled",)] if recorded else [],
        [("delivered" if recorded else "pending", 1)],
    )
    repeated = await recovery.recover_dispatched(
        db=database.get_db() if named else None, find_input=find_input,
        sources=[Source()], now=NOW,
    )
    assert repeated == {"delivered": 0, "retried": 0}
    writer_available(continuation_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("named", [False, True])
async def test_recovery_does_not_finish_a_callback_foreign_write(continuation_db, recorded, named):
    seeded_trigger(dispatched=True)
    db = database.get_db()

    async def find_input(_thread, _key):
        writer_available(continuation_db)
        await asyncio.sleep(0)
        db.execute("INSERT INTO continuation_markers VALUES ('callback')")
        return {} if recorded else None

    try:
        with pytest.raises(database.TransactionOwnershipError):
            await recovery.recover_dispatched(
                db=db if named else None, find_input=find_input, sources=[Source()], now=NOW,
            )
        assert db.in_transaction
        assert stored(continuation_db) == ([], [("dispatched", 1)])
        db.commit()
        assert stored(continuation_db) == ([("callback",)], [("dispatched", 1)])
    finally:
        db.rollback()


@pytest.mark.asyncio
async def test_recovery_rejects_an_active_explicit_owner(continuation_db):
    seeded_trigger(dispatched=True)
    db = database.get_db()
    db.execute("INSERT INTO continuation_markers VALUES ('caller')")

    async def find_input(*_args):
        pytest.fail("recovery awaited inside the caller transaction")

    try:
        with pytest.raises(database.TransactionOwnershipError):
            await recovery.recover_dispatched(db=db, find_input=find_input, sources=[Source()])
        assert db.in_transaction
        assert stored(continuation_db) == ([], [("dispatched", 1)])
    finally:
        db.rollback()


@pytest.mark.asyncio
async def test_failed_recovery_commit_leaves_dispatch_durable(continuation_db):
    seeded_trigger(dispatched=True)
    db = database.get_db()
    with pytest.raises(RuntimeError, match="commit rejected"):
        await recovery.recover_dispatched(db=FailCommit(db), sources=[Source()], now=NOW)
    assert not db.in_transaction
    assert stored(continuation_db) == ([], [("dispatched", 1)])
    writer_available(continuation_db)


@pytest.mark.asyncio
async def test_startup_does_not_adopt_or_close_a_foreign_owner(continuation_db):
    seeded_trigger(dispatched=True)
    db = database.get_db()
    db.execute("INSERT INTO continuation_markers VALUES ('caller')")
    try:
        with pytest.raises(database.TransactionOwnershipError):
            await recovery.startup_recover()
        assert db.in_transaction
        assert stored(continuation_db) == ([], [("dispatched", 1)])
        db.commit()
        assert stored(continuation_db) == ([("caller",)], [("dispatched", 1)])
    finally:
        db.rollback()


@pytest.mark.asyncio
async def test_startup_retires_rows_whose_source_was_removed(continuation_db):
    """A row of a retired kind (the old ``plan_parent`` route) has no settler."""
    live = seeded_trigger(dispatched=True)
    with database.write_transaction(None, scope="continuation_test_retired") as db:
        db.execute(
            """INSERT INTO continuation_triggers
               (id,kind,thread_id,scope_id,dedupe_key,state,created_at,updated_at)
               VALUES ('retired','plan_parent',?,'scope','retired-key','dispatched',?,?)""",
            (live.thread_id, NOW, NOW),
        )

    result = await recovery.startup_recover()

    assert result["retired"] == 1
    with closing(sqlite3.connect(str(continuation_db))) as reader:
        rows = dict(reader.execute(
            "SELECT id,state||':'||COALESCE(reason,'') FROM continuation_triggers",
        ).fetchall())
    assert rows["retired"] == "cancelled:source_retired"
    assert rows[live.id] != "cancelled:source_retired"
    writer_available(continuation_db)


@pytest.mark.asyncio
async def test_standalone_prepare_commits_harvest_before_collection(continuation_db):
    with database.write_transaction(None, scope="continuation_test_project") as db:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('project-1','Project','t','t')",
        )
    _plan_id, node_id = _seed_attachment(db, "parent-a", "child-a")

    class AfterHarvest(Source):
        def due(self, now, *, db):
            with closing(sqlite3.connect(str(continuation_db))) as reader:
                assert reader.execute(
                    "SELECT state FROM plan_nodes WHERE id=?", (node_id,),
                ).fetchone() == ("review_requested",)
            return []

    await runner.sweep(NOW, sources=[HarvestSource(), AfterHarvest()])
    assert service.get_node(node_id, db=db).state == "review_requested"
    writer_available(continuation_db)
