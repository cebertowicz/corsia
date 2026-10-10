"""Canonical PR notices remain once-only across loop and worker consumers."""
from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from events.connectors import github_pr_notice as notices
from events.identity import resolve_identity
from infra.db_async import run_in_db
from infra.settings import get_setting
from tests.event_routing_helpers import mk_session, pr_payload, register_pr


@pytest.fixture
def owned(isolated_db):
    notices._owned_cache.clear()
    notices._last_poll_notice.clear()
    owner = mk_session("notice-owner")
    register_pr("o/r", 1, owner)
    identity = resolve_identity("webhook.github.pr_opened", pr_payload("o/r", 1))
    assert identity is not None
    yield owner, identity
    notices._owned_cache.clear()
    notices._last_poll_notice.clear()


def _marker(owner, identity):
    return f"pr_notice:{owner}:{identity.external_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["native", "bridge"])
async def test_exactly_one_notice_across_tool_and_route_consumers(owned, monkeypatch, seam):
    owner, identity = owned
    assert get_setting(_marker(owner, identity)) is None
    ready = threading.Barrier(2, timeout=5)
    original = notices._claim
    claims = []

    def observed_claim(thread_id, pr):
        claims.append(pr.noticed)
        # Both real ownership queries finish before the canonical writer begins.
        ready.wait()
        return original(thread_id, pr)

    monkeypatch.setattr(notices, "_claim", observed_claim)
    worker = asyncio.create_task(run_in_db(notices.notice_for_routed_event, owner, identity))
    await asyncio.sleep(0)
    arguments = dict(thread_id=owner, tool_name="read_file", arguments={})
    if seam == "native":
        tool_notice = notices.pr_event_notice(**arguments)
    else:
        tool_notice = await asyncio.to_thread(notices.pr_event_notice, **arguments)
    routed_notice = await worker
    assert claims == [False, False]
    count = sum(value is not None for value in (tool_notice, routed_notice))
    assert count == 1, "same marker emitted two ownership notices"
    assert get_setting(_marker(owner, identity)) is not None


def _pause_worker_query(monkeypatch, *, error=False):
    """Delegate real SQL unchanged; pause only after the first worker fetch."""
    import infra.database as database

    main = threading.get_ident()
    fetched, resume = threading.Event(), threading.Event()
    original = database.get_db
    owned_sql = notices.OWNED_PR_SQL.format(thread_filter="AND r.parent_thread_id=?")

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            fetched.set()
            assert resume.wait(5), "worker query resume barrier expired"
            if error:
                raise sqlite3.OperationalError("ownership read failed")
            return rows

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args, **kwargs):
            cursor = self.connection.execute(sql, *args, **kwargs)
            if threading.get_ident() != main and sql == owned_sql and not fetched.is_set():
                return Cursor(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(database, "get_db", lambda: Connection(original()))
    return fetched, resume


@pytest.mark.asyncio
async def test_release_leaves_next_notice_after_an_older_query_finishes(owned, monkeypatch):
    owner, identity = owned
    assert notices.pr_event_notice(thread_id=owner, tool_name="read_file", arguments={})
    fetched, resume = _pause_worker_query(monkeypatch)
    worker = asyncio.create_task(run_in_db(notices.notice_for_routed_event, owner, identity))
    assert await asyncio.to_thread(fetched.wait, 5), "worker never fetched ownership"
    try:
        await run_in_db(notices.release_notice, owner, identity)
        assert get_setting(_marker(owner, identity)) is None
    finally:
        resume.set()
    assert await worker is None
    assert get_setting(_marker(owner, identity)) is None
    assert notices.pr_event_notice(thread_id=owner, tool_name="read_file", arguments={}), (
        "older query suppressed the next notice after release"
    )


@pytest.mark.asyncio
async def test_pending_read_does_not_hide_owned_prs_or_replace_a_newer_read(owned, monkeypatch):
    owner, _identity = owned
    fetched, resume = _pause_worker_query(monkeypatch)
    worker = asyncio.create_task(run_in_db(notices._owned_prs, owner, fresh=True))
    assert await asyncio.to_thread(fetched.wait, 5)
    try:
        current = notices._owned_prs(owner, fresh=False)
        assert [pr.external_id for pr in current] == ["o/r#1"]
        newer = notices._owned_cache[owner]
    finally:
        resume.set()
    assert [pr.external_id for pr in await worker] == ["o/r#1"]
    assert notices._owned_cache[owner] is newer
    assert notices.pr_event_notice(thread_id=owner, tool_name="read_file", arguments={})


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_read", [False, True], ids=["drop-failed", "preserve-newer"])
async def test_failed_read_does_not_poison_cache(owned, monkeypatch, newer_read):
    owner, _identity = owned
    fetched, resume = _pause_worker_query(monkeypatch, error=True)
    worker = asyncio.create_task(run_in_db(notices._owned_prs, owner, fresh=True))
    assert await asyncio.to_thread(fetched.wait, 5)
    try:
        if newer_read:
            current = notices._owned_prs(owner, fresh=True)
            assert [pr.external_id for pr in current] == ["o/r#1"]
            newer = notices._owned_cache[owner]
    finally:
        resume.set()
    with pytest.raises(sqlite3.OperationalError, match="ownership read failed"):
        await worker
    if newer_read:
        assert notices._owned_cache[owner] is newer
    else:
        assert owner not in notices._owned_cache
    assert notices.pr_event_notice(thread_id=owner, tool_name="read_file", arguments={})


def test_notice_render_failure_leaves_no_marker(owned, monkeypatch):
    owner, identity = owned

    def fail_render(pr):
        raise RuntimeError("notice rendering failed")

    with monkeypatch.context() as rendering:
        rendering.setattr(notices, "_notice", fail_render)
        with pytest.raises(RuntimeError, match="notice rendering failed"):
            notices.notice_for_routed_event(owner, identity)
    assert get_setting(_marker(owner, identity)) is None
    assert notices.notice_for_routed_event(owner, identity)


def test_winning_and_losing_claims_invalidate_the_read_cache(owned):
    owner, _identity = owned
    pending = notices._owned_prs(owner, fresh=True)[0]
    assert owner in notices._owned_cache
    assert notices._claim(owner, pending)
    assert owner not in notices._owned_cache
    assert notices._owned_prs(owner, fresh=True)[0].noticed
    assert owner in notices._owned_cache
    assert notices._claim(owner, pending) is None
    assert owner not in notices._owned_cache
