from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

import pytest


def _lock(git_dir: Path, *, age_seconds: int = 600) -> Path:
    git_dir.mkdir(parents=True, exist_ok=True)
    lock = git_dir / "index.lock"
    lock.write_text("", encoding="utf-8")
    then = time.time() - age_seconds
    os.utime(lock, (then, then))
    return lock


def _set_lsof(monkeypatch, tmp_path: Path, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lsof = bin_dir / "lsof"
    lsof.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    lsof.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def test_backend_runner_recovers_stale_lock_with_the_shared_script(
    tmp_path: Path, monkeypatch
) -> None:
    from infra.project_repo_lock_recovery import recover_stale_git_index_lock

    _set_lsof(monkeypatch, tmp_path, "exit 1")
    git_dir = tmp_path / ".git"
    lock = _lock(git_dir)

    assert recover_stale_git_index_lock(git_dir) is True
    assert not lock.exists()


def test_backend_runner_preserves_lock_held_by_a_live_process(
    tmp_path: Path, monkeypatch
) -> None:
    from infra.project_repo_lock_recovery import (
        GitIndexLockRecoveryError,
        recover_stale_git_index_lock,
    )

    git_dir = tmp_path / ".git"
    lock = _lock(git_dir)
    ready = tmp_path / "holder-ready"
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'exec 9> "$1"; touch "$2"; sleep 30',
            "_",
            str(lock),
            str(ready),
        ],
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        then = time.time() - 600
        os.utime(lock, (then, then))
        _set_lsof(monkeypatch, tmp_path, f"printf '%s\\n' {holder.pid}")

        with pytest.raises(GitIndexLockRecoveryError, match="active process.*preserved"):
            recover_stale_git_index_lock(git_dir)

        assert lock.exists()
    finally:
        os.killpg(holder.pid, signal.SIGTERM)
        holder.wait(timeout=3)


def test_missing_shared_script_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from infra import project_repo_lock_recovery as recovery

    git_dir = tmp_path / ".git"
    lock = _lock(git_dir)
    monkeypatch.setattr(recovery, "_shared_lock_recovery_script", lambda: tmp_path / "missing.sh")

    with pytest.raises(
        recovery.GitIndexLockRecoveryError,
        match="could not find the shared recovery rule.*Do not delete it or retry",
    ):
        recovery.recover_stale_git_index_lock(git_dir)

    assert lock.exists()


def test_shared_runner_timeout_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from infra import project_repo_lock_recovery as recovery

    git_dir = tmp_path / ".git"
    lock = _lock(git_dir)

    def _timeout(*_args, **kwargs):
        assert kwargs["timeout"] == recovery.LOCK_RECOVERY_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired("bash", recovery.LOCK_RECOVERY_TIMEOUT_SECONDS)

    monkeypatch.setattr(recovery.subprocess, "run", _timeout)

    with pytest.raises(recovery.GitIndexLockRecoveryError, match="before its timeout"):
        recovery.recover_stale_git_index_lock(git_dir)

    assert lock.exists()
