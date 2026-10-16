"""Real Safehouse runs of a scheduled script (L3, macOS).

Runs real scripts through ``script_sandbox.run_script`` and a real
``ProcessOwner``. Follows the convention in ``test_shell_execution_envelopes``:
skipped inside an agent shell and where Safehouse is missing, and failing in
both cases when ``CORSIA_REQUIRE_SAFEHOUSE=1`` (the L3 job). The default
database is never used: ``isolated_db`` points the DB at ``tmp_path``, the
workspace is a short directory under the system temp root, and the process
owner's runtime directory is fresh.

Startup timings are written to the CI log and carried in the assertion message.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from automation.script_sandbox import STDOUT_CAP_BYTES, run_script

pytestmark = pytest.mark.integration

THREAD_ID = "script-sandbox-it"
#: A pathological Safehouse startup must fail visibly, not hang the job.
FOUR_CONCURRENT_WALL_SECONDS = 10.0


@pytest.fixture
def short_system_temp():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def sandbox(isolated_db, monkeypatch, tmp_path, short_system_temp):
    from tools import sandbox_config, shell_guard, unix_socket_policy

    required = os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1"
    if os.environ.get("CORSIA_THREAD_ID"):
        if required:
            pytest.fail("CORSIA_REQUIRE_SAFEHOUSE=1 cannot run inside an agent shell")
        pytest.skip("agent shells cannot prove nested Safehouse confinement")
    if shell_guard.shutil.which("safehouse") is None:
        if required:
            pytest.fail("CORSIA_REQUIRE_SAFEHOUSE=1 but Safehouse is unavailable")
        pytest.skip("Agent Safehouse is unavailable")
    from automation import script_sandbox
    from host_access.processes import ProcessOwner
    from infra.database import get_db

    workspace = short_system_temp / "w"
    home = short_system_temp / "h"
    directory = workspace / "scripts"
    for path in (directory, home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
    monkeypatch.setattr(
        unix_socket_policy.tempfile, "gettempdir", lambda: str(short_system_temp),
    )
    monkeypatch.setenv("HOME", str(home))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES (?, 'user', 't', 't')",
            (THREAD_ID,),
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        script_sandbox,
        "PROCESS_OWNER",
        ProcessOwner(
            runtime_dir=tmp_path / "runtime",
            term_grace_seconds=0.2,
            kill_grace_seconds=2.0,
        ),
    )
    return directory


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


async def _run(path: Path, *, timeout: int = 20):
    return await run_script(
        THREAD_ID, None, path, run_id=uuid.uuid4().hex, timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_real_script_runs_in_the_sandbox_with_a_turnless_cwd(sandbox) -> None:
    path = _script(
        sandbox, "reading.sh", "pwd\necho '{\"series\":\"cost\",\"value\":3}'\n",
    )
    run = await _run(path)
    lines = run.stdout.decode().splitlines()
    assert run.returncode == 0 and not run.timed_out, run.stderr
    assert Path(lines[0]).resolve() == sandbox.resolve()  # cwd is the script's dir
    assert lines[1] == '{"series":"cost","value":3}'


@pytest.mark.asyncio
async def test_real_script_receives_no_agent_account_environment(sandbox) -> None:
    path = _script(
        sandbox,
        "no-credentials.sh",
        'test -z "$GH_CONFIG_DIR" && test -z "$GIT_CONFIG_COUNT"\n',
    )
    run = await _run(path)
    assert run.returncode == 0, run.stderr


@pytest.mark.asyncio
async def test_real_script_exit_code_and_stderr_are_reported(sandbox) -> None:
    path = _script(sandbox, "fails.sh", "echo oops >&2\nexit 7\n")
    run = await _run(path)
    assert run.returncode != 0 and not run.timed_out
    assert b"oops" in run.stderr


@pytest.mark.asyncio
async def test_real_script_past_its_timeout_is_killed(sandbox) -> None:
    path = _script(sandbox, "hangs.sh", "echo started\nsleep 60\n")
    started = time.monotonic()
    run = await _run(path, timeout=2)
    assert run.timed_out
    assert time.monotonic() - started < 20  # killed, not waited out


@pytest.mark.asyncio
async def test_real_script_output_is_capped_at_64kb(sandbox) -> None:
    path = _script(sandbox, "floods.sh", "yes | head -c 1000000\n")
    run = await _run(path)
    assert len(run.stdout) == STDOUT_CAP_BYTES + 1  # one past the cap marks overflow
    assert run.over_cap


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 4])
async def test_real_script_startup_cost_by_concurrency(
    sandbox, count, capsys,
) -> None:
    path = _script(sandbox, "noop.sh", "echo '{\"series\":\"x\",\"value\":1}'\n")
    started = time.monotonic()
    runs = await asyncio.gather(*(_run(path) for _ in range(count)))
    wall = time.monotonic() - started
    line = f"SAFEHOUSE_STARTUP concurrent={count} wall_seconds={wall:.3f}"
    with capsys.disabled():
        print(f"\n{line}")
    assert all(run.returncode == 0 for run in runs), line
    if count == 4:
        assert wall < FOUR_CONCURRENT_WALL_SECONDS, line
