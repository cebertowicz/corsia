"""CC startup borrowers cannot commit or discard another async frame's work."""

from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
from unittest.mock import AsyncMock

import pytest

from cc_runtime_fixtures import (  # noqa: F401
    StubCCInvocation, cc_target, child_db, prepared_cc_input,
)
from infra import database
from subagents import cc_runtime_resume, cc_spawn


def _pending_owner():
    db = database.get_db()
    db.execute(
        "INSERT INTO settings (key,value,updated_at) "
        "VALUES ('startup-sentinel','pending','t')"
    )
    return db


def _assert_preserved(db):
    assert db.in_transaction
    assert db.execute(
        "SELECT value FROM settings WHERE key='startup-sentinel'"
    ).fetchone()[0] == "pending"
    with closing(sqlite3.connect(str(database.DB_PATH))) as reader:
        assert reader.execute(
            "SELECT value FROM settings WHERE key='startup-sentinel'"
        ).fetchone() is None


@pytest.mark.parametrize(("value", "expected"), [
    (None, None),
    ('{"max_concurrent_live": 3}', {"max_concurrent_live": 3}),
    ('"scalar"', None),
    ("malformed", None),
])
def test_startup_setting_read_preserves_pending_work(child_db, value, expected):
    from infra.settings import get_setting

    if value is not None:
        child_db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES ('read-target',?,'t')",
            (value,),
        )
        child_db.commit()
    db = _pending_owner()
    try:
        assert get_setting("read-target") == expected
        _assert_preserved(db)
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_exists", [False, True])
async def test_main_start_rejection_does_not_own_pending_work(child_db, thread_exists):
    if thread_exists:
        child_db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('main','user','claude-code/opus','t','t')"
        )
        child_db.commit()
    db = _pending_owner()
    try:
        kwargs = dict(
            target=cc_target("sonnet"), surface_exit_error_fn=AsyncMock(),
            finalize_fn=lambda *_args, **_kwargs: None, held_lock=asyncio.Lock(),
        )
        if thread_exists:
            with pytest.raises(ValueError, match="selected model"):
                await cc_runtime_resume.start_cc_for_row("main", **kwargs)
        else:
            assert await cc_runtime_resume.start_cc_for_row("main", **kwargs) is None
        _assert_preserved(db)
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
async def test_child_preparation_rejection_preserves_pending_work(child_db, available):
    child_db.execute(
        "UPDATE connections SET status=? WHERE provider='claude-code'",
        ("available" if available else "not_installed",),
    )
    child_db.commit()
    db = _pending_owner()
    try:
        reason = "not found" if available else "not available"
        with pytest.raises(ValueError, match=reason):
            await cc_spawn.spawn(
                "missing-parent", "task", binary="unused",
                target=cc_target("sonnet"), broadcast=lambda *_args: None,
            )
        _assert_preserved(db)
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_after_start", [False, True])
async def test_main_launch_commits_its_facts_not_later_async_work(
    child_db, tmp_path, monkeypatch, pending_after_start,
):
    from registry.listing import find_for_thread
    from subagents import cc_destination, cc_runtime, cc_startup_process
    from subagents.cc_runtime_quiescence import cleanup_ephemeral_live

    child_db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('main','user','claude-code/sonnet','t','t')"
    )
    child_db.commit()
    started = asyncio.Event()
    release = asyncio.Event()
    observations = []

    class Session(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            with closing(sqlite3.connect(str(database.DB_PATH), timeout=0.05)) as reader:
                reader.row_factory = sqlite3.Row
                identity = find_for_thread("main", "claude-code", "session", db=reader)
                assert identity["external_id"] == self.session_id
                assert "claude_invocation" in identity["metadata"]
                assert reader.execute(
                    "SELECT COUNT(*) FROM messages WHERE thread_id='main' "
                    "AND json_extract(metadata,'$.type')='seed'"
                ).fetchone()[0] == 1
                reader.execute("BEGIN IMMEDIATE")
                reader.rollback()
            observations.append("durable and unlocked before start")
            started.set()
            await release.wait()

        async def terminate(self):
            self.returncode = 0

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", Session)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr("subagents.cc_runtime_helpers._validate_working_dir", lambda *_args: None)
    target = cc_target("sonnet")
    prepared = await prepared_cc_input("main", "hello", target)
    lock = cc_runtime._LIVE.lock("main")
    await lock.acquire()
    launch = asyncio.create_task(cc_runtime_resume.start_cc_for_row(
        "main", target=target, binary="unused", current_user_text="hello",
        prepared_context=prepared, claude_dir=str(tmp_path / "claude"),
        broadcast=lambda *_args: None, surface_exit_error_fn=AsyncMock(),
        finalize_fn=cc_runtime._finalize, held_lock=lock,
    ))
    db = None
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        if pending_after_start:
            db = _pending_owner()
        release.set()
        live = await asyncio.wait_for(launch, timeout=3)
        assert live is not None
        assert observations == ["durable and unlocked before start"]
        if db is not None:
            _assert_preserved(db)
    finally:
        release.set()
        if not launch.done():
            launch.cancel()
        await asyncio.gather(launch, return_exceptions=True)
        lock.release()
        if db is not None:
            db.rollback()
            db.close()
        live = cc_runtime._LIVE.pop("main", None)
        if live is not None:
            cleanup_ephemeral_live(live, thread_id="main")
