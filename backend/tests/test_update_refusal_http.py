"""The detached pre-stop owner reports its refusal over the real drain route."""

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from auth_fixtures import OWNER_TOKEN
from automation.update_log import record_update_log


@pytest.mark.asyncio
@pytest.mark.parametrize("case,healthy,claimed_healthy,reason", [
    ("lock", True, True, "another update or start"),
    ("unhealthy", False, False, "current backend is not healthy"),
    ("ownership", True, False, "foreign service ownership"),
    ("missing", True, True, "There is no staged update"),
])
async def test_packaged_pre_stop_refusal_reports_over_http(isolated_db, tmp_path, monkeypatch, case, healthy, claimed_healthy, reason):
    from api.drain import router
    from api.restart_shell import spawn_restart_lifecycle
    from automation import self_restart
    from infra import config
    from infra.db_async import run_in_db
    from infra.update_operation import project_update_operation

    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr("infra.drain.cancel_drain_projection", lambda: None)
    monkeypatch.setattr("automation.restart_settlement.resume_threads_detached", lambda *_args: None)
    monkeypatch.setattr("automation.update_failure_notice.tell_conductor_detached", lambda *_args: None)
    operation_id = "b" * 32
    log = tmp_path / f"update-{operation_id}.log"
    assert self_restart.try_acquire(None, "steve", None, None, "update", kind="update", operation_id=operation_id)
    app = FastAPI()
    app.include_router(router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    root = Path(__file__).resolve().parents[2]
    corsia_text = (root / "scripts/corsia").read_text()
    fail = next(line for line in corsia_text.splitlines() if line.startswith("fail() {"))
    app_mode = root / "scripts/corsia.d/app_mode.sh"
    runtime = root / "scripts/corsia.d/runtime.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{app_mode}"
source "{runtime}"
RED=""; NC=""
{fail}
require_owned_service_definition() {{ { 'fail "foreign service ownership"; return 1' if case == 'ownership' else ':' }; }}
service_is_loaded() {{ return 0; }}
require_loaded_service_owns_port() {{ return {1 if case == 'ownership' else 0}; }}
backend_pids() {{ :; }}
require_no_unmanaged_backend() {{ :; }}
app_mode_hold_update_lock() {{ return {1 if case == 'lock' else 0}; }}
is_backend_running() {{ return {0 if healthy else 1}; }}
app_mode_staged_path() {{ printf '%s' '/disposable/no-stage'; }}
app_mode_previous_path() {{ printf '%s' '/disposable/no-previous'; }}
FORCE=false
BACKEND_PORT={port}
{ 'app_mode_restart_prepared' if case == 'missing' else 'app_mode_swap_staged_update /disposable/staged' }
''')
    script.chmod(0o755)
    try:
        with log.open("w") as out:
            assert record_update_log(operation_id, str(log))
            child = spawn_restart_lifecycle(script, ["restart-prepared"], {
                **os.environ, "CORSIA_RESTART_BACKEND_PORT": str(port),
            }, operation_id, out, caller_credential=OWNER_TOKEN)
            assert await asyncio.to_thread(child.wait, 10) != 0
    finally:
        server.should_exit = True
        await serving
        listener.close()
    row = await run_in_db(self_restart.operation_by_id, operation_id)
    assert row["status"] == "failed", log.read_text()
    assert row["failure_code"] == "update_refused"
    assert reason in row["error"]
    assert json.loads(row["failure_detail"])["backend_healthy"] is claimed_healthy
    assert reason in project_update_operation(row)["log_tail"]
    assert not self_restart.restart_holds_new_work()


def test_failure_after_preflight_is_not_reported_as_refusal(tmp_path, monkeypatch):
    from api.restart_shell import spawn_restart_lifecycle

    root = Path(__file__).resolve().parents[2]
    text = (root / "scripts/corsia").read_text()
    fail = next(line for line in text.splitlines() if line.startswith("fail() {"))
    app_mode = root / "scripts/corsia.d/app_mode.sh"
    runtime = root / "scripts/corsia.d/runtime.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{app_mode}"
source "{runtime}"
RED=""; NC=""
{fail}
require_owned_service_definition() {{ :; }}
require_loaded_service_owns_port() {{ :; }}
service_is_loaded() {{ return 0; }}
is_backend_running() {{ return 0; }}
app_mode_hold_update_lock() {{ :; }}
drain_and_wait() {{ :; }}
bootout_service_and_wait() {{ return 1; }}
service_domain() {{ :; }}
service_label() {{ :; }}
current_drain_url() {{ :; }}
cancel_failed_drain() {{ :; }}
FORCE=false
BACKEND_PORT=4111
app_mode_swap_staged_update /disposable/staged
''')
    script.chmod(0o755)
    trace = tmp_path / "calls"
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$TRACE"\n')
    curl.chmod(0o755)
    monkeypatch.setattr("api.restart_shell.shutil.which", lambda *_args, **_kwargs: str(curl))
    log = tmp_path / "update.log"
    with log.open("w") as out:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], {
            **os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111", "TRACE": str(trace),
        }, "a" * 32, out, caller_credential=OWNER_TOKEN)
        assert child.wait(timeout=5) != 0
    assert "failure_code=update_refused" not in trace.read_text()
