"""Restart settlement retains the hold when the final record cannot be published."""

import asyncio
import os
from pathlib import Path

from api.restart_shell import spawn_restart_lifecycle
from api.system_admin import _monitor_restart_process
from automation import self_restart
from infra.db_async import run_in_db
from test_packaged_update_lifecycle import operation_in


async def test_failed_final_record_write_keeps_teardown_hold(isolated_db, tmp_path):
    operation_id = await run_in_db(operation_in, "restarting")
    service = Path(__file__).resolve().parents[2] / "scripts/corsia.d/service.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{service}"
fail() {{ :; }}
launchctl_bootout_wait() {{ touch "$TEARDOWN_STARTED"; return 124; }}
mv() {{ [ ! -e "$TEARDOWN_STARTED" ] || return 1; /bin/mv "$@"; }}
bootout_service_and_wait domain label
''')
    script.chmod(0o755)
    environment = {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111",
                   "TEARDOWN_STARTED": str(tmp_path / "started")}
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], environment,
                                        operation_id, log, caller_credential="test-credential")
        assert await asyncio.to_thread(child.wait, 5) == 2
        assert (tmp_path / "started").exists()
        await _monitor_restart_process(child, operation_id)
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "in_flight"
    assert self_restart.restart_holds_new_work()


async def test_failed_initial_record_write_prevents_bootout(isolated_db, tmp_path):
    operation_id = await run_in_db(operation_in, "restarting")
    service = Path(__file__).resolve().parents[2] / "scripts/corsia.d/service.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{service}"
fail() {{ :; }}
launchctl_bootout_wait() {{ touch "$TEARDOWN_STARTED"; }}
mv() {{ return 1; }}
bootout_service_and_wait domain label
''')
    script.chmod(0o755)
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"],
            {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111",
             "TEARDOWN_STARTED": str(tmp_path / "started")},
            operation_id, log, caller_credential="test-credential",
        )
        assert await asyncio.to_thread(child.wait, 5) == 1
        assert not (tmp_path / "started").exists()
        await _monitor_restart_process(child, operation_id)
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "failed" and row["failure_code"] == "launch_failed"
    assert not self_restart.restart_holds_new_work()


async def test_signal_during_bootout_keeps_teardown_hold(isolated_db, tmp_path):
    operation_id = await run_in_db(operation_in, "restarting")
    service = Path(__file__).resolve().parents[2] / "scripts/corsia.d/service.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{service}"
fail() {{ :; }}
launchctl_bootout_wait() {{ kill -TERM "$$"; }}
bootout_service_and_wait domain label
''')
    script.chmod(0o755)
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"],
            {**os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111"},
            operation_id, log, caller_credential="test-credential",
        )
        assert await asyncio.to_thread(child.wait, 5) == 143
        await _monitor_restart_process(child, operation_id)
    assert (await run_in_db(self_restart.operation_by_id, operation_id))["status"] == "in_flight"
    assert self_restart.restart_holds_new_work()
