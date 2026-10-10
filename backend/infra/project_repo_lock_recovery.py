"""Run the shared fail-closed Git index-lock recovery before fast-forwarding."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


LOCK_RECOVERY_TIMEOUT_SECONDS = 15
_RECOVERY_COMMAND = """
fail() { printf '%s\\n' "$*" >&2; }
warn() { printf '%s\\n' "$*" >&2; }
source "$1"
recover_stale_git_index_lock "$2"
"""


class GitIndexLockRecoveryError(RuntimeError):
    """Git index-lock recovery could not safely prove removal is allowed."""


def _preserved_lock_error(lock: Path, checkout: Path, detail: str) -> GitIndexLockRecoveryError:
    """Build one actionable, fail-closed error for every preserved lock state."""
    return GitIndexLockRecoveryError(
        f"Git index lock at {lock} in checkout {checkout} {detail}; it was preserved. "
        "Do not delete it or retry this command; report the checkout path and this "
        "error to the task owner."
    )


def _shared_lock_recovery_script() -> Path:
    """Locate the shared rule from the backend checkout that owns this process."""
    return Path(__file__).resolve().parents[2] / "scripts" / "corsia.d" / "git_lock_recovery.sh"


def _recovery_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Return bounded, one-line shared-script failure context."""
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    return detail[:500] or "the shared recovery rule returned no failure detail"


def recover_stale_git_index_lock(git_dir: Path) -> bool:
    """Run the one shared rule and report unsafe recovery states to the caller."""
    lock = git_dir / "index.lock"
    if not lock.exists():
        return False
    checkout = git_dir.parent
    script = _shared_lock_recovery_script()
    if not script.is_file():
        raise _preserved_lock_error(lock, checkout, "could not find the shared recovery rule")
    try:
        result = subprocess.run(
            ["bash", "-c", _RECOVERY_COMMAND, "_", str(script), str(git_dir)],
            capture_output=True,
            check=False,
            env={**os.environ, "CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX": ""},
            text=True,
            timeout=LOCK_RECOVERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise _preserved_lock_error(
            lock,
            checkout,
            "could not complete the shared recovery rule before its timeout",
        ) from exc
    except OSError as exc:
        raise _preserved_lock_error(
            lock,
            checkout,
            "could not start the shared recovery rule",
        ) from exc
    if result.returncode:
        raise _preserved_lock_error(
            lock,
            checkout,
            f"could not safely complete the shared recovery rule ({_recovery_detail(result)})",
        )
    return True
