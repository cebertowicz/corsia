"""Activity timestamps obey the declared owner, including their Retry caller."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest


@pytest.fixture
def owner(isolated_db, monkeypatch):
    from infra.database import get_db
    from shared_send_fixtures import _insert_native_model

    _insert_native_model()
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
        "VALUES ('owner','user','Owner','native/test','t0','t0')"
    )
    db.commit()
    db.close()
    monkeypatch.setattr("infra.queries.utc_now_iso", lambda: "t1")
    return isolated_db


def _row(path):
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT title,updated_at FROM threads WHERE id='owner'"
        ).fetchone()


def test_standalone_touch_commits_the_timestamp(owner):
    from infra.queries import touch_thread

    touch_thread("owner")
    assert _row(owner) == ("Owner", "t1")


def test_standalone_touch_refuses_to_commit_another_owner(owner):
    from infra.database import TransactionOwnershipError, get_db
    from infra.queries import touch_thread

    db = get_db()
    db.execute("UPDATE threads SET title='owner-work' WHERE id='owner'")
    try:
        with pytest.raises(TransactionOwnershipError):
            touch_thread("owner")
        assert db.in_transaction
        assert _row(owner) == ("Owner", "t0")
        db.commit()
        assert _row(owner) == ("owner-work", "t0")
    finally:
        db.rollback()
        db.close()


def test_clean_named_connection_delegates_commit_but_not_close(owner):
    from infra.queries import touch_thread

    with closing(sqlite3.connect(str(owner))) as db:
        touch_thread("owner", db=db)
        assert not db.in_transaction
        assert _row(owner) == ("Owner", "t1")
        assert db.execute("SELECT 1").fetchone() == (1,)


def test_named_owner_keeps_rollback_of_its_timestamp(owner):
    from infra.queries import touch_thread

    with closing(sqlite3.connect(str(owner))) as db:
        db.execute("UPDATE threads SET title='owner-work' WHERE id='owner'")
        touch_thread("owner", db=db)
        assert db.in_transaction
        assert _row(owner) == ("Owner", "t0")
        assert db.execute(
            "SELECT title,updated_at FROM threads WHERE id='owner'"
        ).fetchone() == ("owner-work", "t1")
        db.rollback()
    assert _row(owner) == ("Owner", "t0")


def test_failed_nested_touch_rolls_back_only_its_own_changes(owner):
    from infra.database import get_db
    from infra.queries import touch_thread

    db = get_db()
    db.execute(
        "CREATE TRIGGER reject_touch BEFORE UPDATE OF updated_at ON threads "
        "BEGIN UPDATE threads SET title='inner-leak' WHERE id=NEW.id; "
        "SELECT RAISE(FAIL, 'touch unavailable'); END"
    )
    db.execute("UPDATE threads SET title='owner-work' WHERE id='owner'")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="touch unavailable"):
            touch_thread("owner", db=db)
        assert db.in_transaction
        db.commit()
        assert _row(owner) == ("owner-work", "t0")
    finally:
        db.rollback()
        db.close()


def test_failed_standalone_commit_releases_its_write(owner, monkeypatch):
    from infra import database, queries

    db = database.get_db()

    class CommitFailure(database._NoOpClose):
        def commit(self):
            raise sqlite3.OperationalError("commit unavailable")

    failing = CommitFailure(db._conn)
    monkeypatch.setattr(database, "get_db", lambda: failing)
    monkeypatch.setattr(queries, "get_db", lambda: failing)
    try:
        with pytest.raises(sqlite3.OperationalError, match="commit unavailable"):
            queries.touch_thread("owner")
        assert not db.in_transaction
        assert _row(owner) == ("Owner", "t0")
        with closing(sqlite3.connect(str(owner), timeout=0.05)) as peer:
            peer.execute("BEGIN IMMEDIATE")
            peer.rollback()
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_owner", [False, True])
async def test_retry_uses_the_standalone_timestamp_owner(owner, foreign_owner):
    from infra.database import get_db
    from infra.db_async import run_in_db
    from infra.queries import get_thread_for_user, touch_thread
    from ws.message_dispatch import handle_message

    frames, ownership = [], []
    retrying = False

    class Socket:
        async def send_json(self, payload):
            frames.append(payload)

    async def send(_thread_id, _row, _content, **options):
        nonlocal retrying
        assert options["persist_input"] is False
        assert options["touch_thread_on_insert"] is True
        retrying = True
        await options["after_persist_before_dispatch"](None)
        return SimpleNamespace(
            turn_id="retry-turn", turn_order=2,
            message_id="retry-input", created_at="t0",
        )

    async def run_job(callback):
        def job():
            if not foreign_owner or not retrying:
                return callback()
            db = get_db()
            db.execute("UPDATE threads SET title='owner-work' WHERE id='owner'")
            try:
                return callback()
            finally:
                ownership.append((db.in_transaction, _row(owner)))
                db.rollback()
                db.close()
        return await run_in_db(job)

    await handle_message(
        Socket(), {"id": "user"}, "user",
        {"thread_id": "owner", "input_id": "retry-input",
         "content": "retry content", "is_retry": True},
        get_db_fn=get_db, run_in_db_fn=run_job,
        get_thread_for_user_fn=get_thread_for_user, touch_thread_fn=touch_thread,
        send_thread_input_fn=send,
        handle_cc_main_message_fn=None, handle_codex_main_message_fn=None,
    )
    assert len(frames) == 1
    assert frames[0]["input_id"] == "retry-input"
    if foreign_owner:
        assert ownership == [(True, ("Owner", "t0"))]
        assert frames[0]["type"] == "error"
        assert _row(owner) == ("Owner", "t0")
    else:
        assert frames[0]["type"] == "input_accepted"
        assert frames[0]["turn_id"] == "retry-turn"
        assert _row(owner) == ("Owner", "t1")


@pytest.mark.asyncio
async def test_native_safe_boundary_commits_touch_before_provider_write(owner):
    from infra.input_admission import accept_thread_input
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_inputs import start_idle_turn
    from ws.native_inbox_batch import native_safe_boundary_input

    accept_thread_input(
        "owner", role="user", content="initial", intent="apply_now",
        message_id="initial",
    )
    initial = start_idle_turn(
        "owner", turn_id="running-turn", boot_epoch="test", started_at="t0",
    )
    assert initial is not None
    begin_batch_write(initial, attempt_id="initial-write", started_at="t0")
    settle_batch_write("initial-write", outcome="accepted")
    accept_thread_input(
        "owner", role="user", content="steer", intent="apply_now",
        message_id="steer", delivery_options={"touch_session_on_insert": True},
    )
    candidate = await native_safe_boundary_input(
        thread_id="owner", thread_row={"id": "owner", "user_id": "user"},
        owner_turn_id="running-turn", max_input_tokens=100_000,
    )
    assert candidate is not None
    assert _row(owner) == ("Owner", "t0")
    await candidate.before_provider_write()
    assert _row(owner) == ("Owner", "t1")
    with closing(sqlite3.connect(str(owner), timeout=0.05)) as peer:
        peer.execute("BEGIN IMMEDIATE")
        state = peer.execute(
            "SELECT delivery_state,write_outcome,write_started_at,delivery_attempt_id "
            "FROM messages WHERE id='steer'"
        ).fetchone()
        assert state[:2] == ("queued", None)
        assert state[2] is not None and state[3] is not None
        peer.rollback()
    await candidate.settle_delivery()
    with closing(sqlite3.connect(str(owner))) as peer:
        assert peer.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='steer'"
        ).fetchone() == ("delivered", "accepted")
