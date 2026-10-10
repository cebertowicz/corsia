"""A write transaction must never open on the event-loop thread."""

from __future__ import annotations

import asyncio
import logging

import pytest

import infra.db_loop_guard as db_loop_guard
from infra import database
from infra.db_async import run_in_db, run_in_db_unless_owned


def _write() -> None:
    with database.write_transaction(database.get_db(), scope="loop_guard_test") as db:
        db.execute("CREATE TABLE IF NOT EXISTS loop_guard_probe (id INTEGER)")


@pytest.fixture(autouse=True)
def _fresh_sites(monkeypatch):
    monkeypatch.setattr(db_loop_guard, "_loop_write_sites", set())


@pytest.mark.asyncio
async def test_strict_mode_raises_on_the_loop_thread(isolated_db, monkeypatch):
    monkeypatch.setenv(db_loop_guard.LOOP_WRITE_STRICT_ENV, "1")
    with pytest.raises(db_loop_guard.LoopThreadWriteError, match="event-loop thread"):
        _write()


@pytest.mark.asyncio
async def test_strict_mode_allows_a_pool_thread(isolated_db, monkeypatch):
    monkeypatch.setenv(db_loop_guard.LOOP_WRITE_STRICT_ENV, "1")
    await run_in_db(_write)


def test_strict_mode_allows_a_plain_thread(isolated_db, monkeypatch):
    monkeypatch.setenv(db_loop_guard.LOOP_WRITE_STRICT_ENV, "1")
    _write()


@pytest.mark.asyncio
async def test_default_logs_once_per_call_site_and_still_writes(isolated_db, caplog):
    caplog.set_level(logging.ERROR, logger=db_loop_guard.log.name)
    for _ in range(3):
        _write()
    hits = [r for r in caplog.records if "event-loop thread" in r.getMessage()]
    assert len(hits) == 1
    assert "test_loop_write_guard.py" in hits[0].getMessage()
    row = database.get_db().execute(
        "SELECT name FROM sqlite_master WHERE name='loop_guard_probe'"
    ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_owned_connection_runs_inline_and_none_leaves_the_loop(isolated_db):
    loop_thread = asyncio.get_running_loop()
    import threading

    main = threading.get_ident()
    seen: list[int] = []

    def probe():
        seen.append(threading.get_ident())

    await run_in_db_unless_owned(object(), probe)
    await run_in_db_unless_owned(None, probe)
    assert seen[0] == main and seen[1] != main
    assert loop_thread is asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_upload_registers_off_the_loop_under_strict(main_db, monkeypatch, tmp_path):
    import io

    from fastapi import UploadFile

    from api import uploads
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _auth_input: {"id": "steve"})
    monkeypatch.setenv(db_loop_guard.LOOP_WRITE_STRICT_ENV, "1")

    uploaded = await uploads.upload_file(
        None,
        UploadFile(filename="a.txt", file=io.BytesIO(b"x"), headers={"content-type": "text/plain"}),
        thread_id="main-1",
    )
    assert uploaded["file_id"]
