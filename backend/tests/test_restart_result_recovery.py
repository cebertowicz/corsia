"""Detached settlement failures and startup result cleanup."""

import asyncio
from types import SimpleNamespace

from automation import restart, self_restart
from infra.db_async import run_in_db
from test_packaged_update_lifecycle import operation_in


async def test_detached_settlement_exception_is_logged(monkeypatch):
    from api import system_admin

    logged = asyncio.Event()
    entries = []

    async def broken_settlement(*_args):
        raise RuntimeError("settlement failed")

    def log_exception(message, operation_id):
        entries.append((message, operation_id))
        logged.set()

    monkeypatch.setattr(system_admin, "settle_restart_exit", broken_settlement)
    monkeypatch.setattr(system_admin, "logger", SimpleNamespace(exception=log_exception))
    child = SimpleNamespace(poll=lambda: None, wait=lambda: 9)
    await system_admin._monitor_restart_process(child, "a" * 32)
    await asyncio.wait_for(logged.wait(), 10)
    assert entries == [("restart exit settlement failed for operation {}", "a" * 32)]
    assert not system_admin._RESTART_EXIT_TASKS


async def test_startup_removes_only_terminal_result_files(isolated_db, monkeypatch, tmp_path):
    from automation import restart_startup, update_verification
    from infra import config

    terminal_id = await run_in_db(operation_in, "ready")
    await restart.abort_restart_control(terminal_id, error="refused", failure_code="update_refused")
    active_id = await run_in_db(operation_in, "ready")
    terminal = tmp_path / f"lifecycle-{terminal_id}.result"
    active = tmp_path / f"lifecycle-{active_id}.result"
    unknown = tmp_path / f"lifecycle-{'b' * 32}.result"
    invalid = tmp_path / "lifecycle-not-an-operation.result"
    for path in (terminal, active, unknown, invalid):
        path.write_text("result")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)

    async def no_work(*_args):
        return None

    monkeypatch.setattr(restart_startup, "drive_startup_continuity", no_work)
    monkeypatch.setattr(update_verification, "settle_unconfirmed_update", no_work)
    logger = SimpleNamespace(info=lambda *_: None, error=lambda *_: None,
                             exception=lambda *_: None)
    await restart_startup.run_startup_drive(no_work(), None, (), None, logger)
    assert not terminal.exists()
    assert active.exists() and unknown.exists() and invalid.exists()
    assert (await run_in_db(self_restart.operation_by_id, active_id))["status"] == "in_flight"
