"""T6 (managed part): before the data folder moves, CORSIA's recorded managed
processes are stopped through their fingerprint-checked process group only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]


def _orphan() -> int:
    """A session leader whose parent has exited, like a job that outlived the backend."""
    launcher = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], start_new_session=True,\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
    )
    return int(subprocess.run([sys.executable, "-c", launcher], capture_output=True, text=True,
                              check=True, timeout=30).stdout)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _record(db: Path, handle: str, pid: int, fingerprint: str) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO managed_process_handles (handle, thread_id, turn_id, long_running, pid, pgid, "
        "creation_fingerprint, log_path, state, started_at) VALUES (?, ?, 'turn', 1, ?, ?, ?, ?, 'running', ?)",
        (handle, f"thread-{handle}", pid, pid, fingerprint, f"/old/{handle}.log", handle),
    )
    connection.commit()
    connection.close()


def test_recorded_processes_stop_by_fingerprint_and_rows_are_kept(tmp_path) -> None:
    from host_access.processes import _process_creation_fingerprint
    from infra.managed_process_schema import MANAGED_PROCESS_DDL

    state = tmp_path / "com.cebertowicz.corsia"
    state.mkdir()
    db = state / "corsia.db"
    connection = sqlite3.connect(db)
    for statement in MANAGED_PROCESS_DDL:
        connection.execute(statement)
    connection.commit()
    connection.close()
    ours, reused = _orphan(), _orphan()
    try:
        _record(db, "a-ours", ours, _process_creation_fingerprint(ours))
        # Same pgid, different process: the pid was reused, so it is not ours to signal.
        _record(db, "b-reused", reused, "0" * 64)

        result = subprocess.run(
            [sys.executable, "-I", "-B", str(BACKEND / "automation" / "stop_recorded_processes.py"), str(state)],
            capture_output=True, text=True, timeout=60, cwd=tmp_path,
        )

        assert result.returncode == 0, result.stderr
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        assert lines == [
            {"handle": "a-ours", "result": "stopped", "thread_id": "thread-a-ours"},
            {"handle": "b-reused", "result": "already_gone", "thread_id": "thread-b-reused"},
        ]
        deadline = time.monotonic() + 10
        while _alive(ours) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(ours)
        assert _alive(reused)
        rows = sqlite3.connect(db).execute("SELECT handle, state FROM managed_process_handles").fetchall()
        assert sorted(rows) == [("a-ours", "running"), ("b-reused", "running")]
    finally:
        for pid in (ours, reused):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
