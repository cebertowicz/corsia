"""Actual pre-protocol startup accepts every state a packaged rollback can inherit."""

from pathlib import Path
import sys
import types

import pytest
from loguru import logger

from automation import self_restart
from infra.database import get_db
from infra.db_async import run_in_db

FIXTURES = Path(__file__).parent / "fixtures" / "update_pre_protocol"


def load_snapshot(name, monkeypatch):
    module = types.ModuleType(f"automation.{name}")
    source = FIXTURES / f"{name}.py.txt"
    exec(compile(source.read_text(), str(source), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, f"automation.{name}", module)
    import automation

    monkeypatch.setattr(automation, name, module)
    return module


def persisted_operation(phase, status):
    operation_id = self_restart.try_acquire(
        None, None, None, None, "update", kind="update", target_version="0.4.2",
        previous_version="0.4.1",
    )
    with get_db() as db:
        db.execute("UPDATE self_restart_requests SET phase=?,status=? WHERE id=?", (phase, status, operation_id))
    return operation_id


@pytest.mark.parametrize("phase,status", [
    ("holding", "in_flight"), ("steering", "in_flight"), ("draining", "in_flight"),
    ("ready", "in_flight"), ("restarting", "in_flight"), ("resuming", "in_flight"),
    ("verifying", "in_flight"), (None, "complete"), (None, "failed"),
    (None, "cancelled"), (None, "superseded"),
])
async def test_pre_protocol_startup_does_not_crash_or_hold_ingress(isolated_db, monkeypatch, phase, status):
    operation_id = await run_in_db(persisted_operation, phase, status)
    monkeypatch.setattr("infra.restart_authorization.serving_head_sha", lambda: "")
    load_snapshot("restart_rule", monkeypatch)
    old_owner = load_snapshot("self_restart", monkeypatch)
    old_startup = load_snapshot("restart_startup", monkeypatch)

    await old_startup.reconcile_startup_continuity(logger)

    assert not old_owner.restart_holds_new_work()
    row = self_restart.operation_by_id(operation_id)
    if phase == "verifying":
        # An unknown phase is ignored. Its pending outcome is an explicit limit.
        assert row["status"] == "in_flight" and row["phase"] == "verifying"
    elif status == "in_flight":
        # Transitional residual: without an answering new backend, old startup
        # marks pre-bounce rows complete even when the installation failed.
        assert row["status"] == "complete"
    else:
        assert row["status"] == status
