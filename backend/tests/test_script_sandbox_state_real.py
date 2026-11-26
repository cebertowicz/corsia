"""A script run can't write CORSIA's database, even with the full ``corsia event emit`` command (L3, macOS).

A script's events come only from its output lines, which the server admits after the run. This proves the
other route is closed: the run reaches the real ``corsia`` CLI through its own client's fallback, and the
CLI's write to a database kept where an install keeps its state (under the runner's home, outside the
workspace and every temp folder) is refused. The control runs the same command, with the same database,
outside the sandbox and sees it write, so the refusal is the sandbox's and not a missing file. Markers
show the run's Python started and could read CORSIA's source, so a run that never reached the database
fails this test instead of passing it.

Skipped inside an agent shell and where Safehouse is missing, like ``test_script_sandbox_real``: it
writes under the real home, which only the L3 job may do.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from automation.script_sandbox import run_script
from tests.test_script_sandbox_real import THREAD_ID, _account, _script, sandbox, short_system_temp  # noqa: F401

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[1]
STARTED = b"probe: python started"
READABLE = b"probe: source readable"


def _install_like_state() -> Path:
    """A state root where an install keeps one: under the user's real home, never temp."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / "Library" / "Application Support" / f"CORSIA-l3-probe-{uuid.uuid4().hex[:12]}" / "state"


def _counts(db_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db_path)) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}


def _shim(directory: Path, state: Path, workspace: Path, home: str) -> Path:
    """The ``corsia`` CLI as an install runs it, pointed at the install-like state root."""
    code = ("import sys; sys.stderr.write('probe: python started\\n'); sys.stderr.flush(); "
            f"open({str(BACKEND / 'cli.py')!r}, 'rb').read(1); "
            "sys.stderr.write('probe: source readable\\n'); sys.stderr.flush(); "
            f"sys.path.insert(0, {str(BACKEND)!r}); import cli; sys.exit(cli.main(sys.argv[1:]))")
    shim = directory / "corsia"
    shim.write_text(
        "#!/bin/sh\n"
        f"export CORSIA_STATE_DIR={shlex.quote(str(state))} CORSIA_WORKSPACE={shlex.quote(str(workspace))}\n"
        f"export HOME={shlex.quote(home)} CORSIA_TEST_MODE=1\n"
        f'exec {shlex.quote(sys.executable)} -c {shlex.quote(code)} "$@"\n'
    )
    shim.chmod(0o755)
    return shim


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_a_runs_corsia_event_emit_cannot_write_the_database(sandbox, monkeypatch, authenticated):
    from connections import github_agent
    from infra import database

    if authenticated:
        _account(monkeypatch, "ready", Path(os.environ["HOME"]) / "agent-gh")
        monkeypatch.setattr(github_agent, "script_forwarder_credential", lambda view: "synthetic-secret")
    state = _install_like_state()
    state.mkdir(parents=True)
    try:
        db_path = state / "corsia.db"
        with closing(sqlite3.connect(database.DB_PATH)) as source, closing(sqlite3.connect(db_path)) as copy:
            source.backup(copy)
        bin_dir = sandbox / "bin"
        bin_dir.mkdir()
        shim = _shim(bin_dir, state, sandbox.parent, os.environ["HOME"])
        emit = ["event", "emit", "qa.sandbox_probe", "--facts", "{}", "--as-session", THREAD_ID]

        # Control: outside the sandbox the same command writes to this database.
        before = _counts(db_path)
        control = subprocess.run([str(shim), *emit, "--id", "control"], capture_output=True, timeout=60,
                                 env={"PATH": os.environ.get("PATH", "")})
        assert STARTED in control.stderr and READABLE in control.stderr, control.stderr
        assert _counts(db_path) != before, ("control wrote nothing, so a refusal below would prove nothing",
                                            control.returncode, control.stderr)

        # The run reaches the real CLI through its own client's fallback, last on PATH.
        argv = " ".join(shlex.quote(word) for word in [*emit, "--id", "sandboxed"])
        script = _script(sandbox, "emit.sh", f'export PATH="$PATH":{shlex.quote(str(bin_dir))}\n'
                                             f"corsia {argv}\n")
        before = _counts(db_path)
        run = await run_script(THREAD_ID, None, script, run_id=uuid.uuid4().hex, timeout_seconds=60,
                               authenticated=authenticated)
        assert STARTED in run.stderr and READABLE in run.stderr, ("the run never reached CORSIA's code",
                                                                  run.returncode, run.stderr)
        assert _counts(db_path) == before, run.stderr
        assert run.returncode != 0, run.stderr
    finally:
        shutil.rmtree(state.parent, ignore_errors=True)
