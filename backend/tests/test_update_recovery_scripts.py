from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


_LOCK_PREAMBLE = """
set -e
fail() { printf 'FAIL:%s\n' "$*" >&2; }
warn() { printf 'WARN:%s\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
"""


def _lock_paths(tmp_path: Path, *, age_seconds: int = 600) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    git_dir = checkout / ".git"
    git_dir.mkdir(parents=True)
    lock = git_dir / "index.lock"
    lock.write_text("")
    old = time.time() - age_seconds
    os.utime(lock, (old, old))
    return git_dir, lock


def _recover(git_dir: Path, body: str = "") -> subprocess.CompletedProcess[str]:
    return _run_bash(
        _LOCK_PREAMBLE
        + body
        + """
recover_stale_git_index_lock "$GIT_DIR"
""",
        env={
            "GIT_DIR": str(git_dir),
            "TRACE": str(git_dir.parents[1] / "trace"),
            "IDENTITY_COUNT": str(git_dir.parents[1] / "identity-count"),
            "OLD_MTIME": "1",
        },
    )


def test_lock_identity_is_stable_across_quarantine_rename(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path)
    quarantine = Path(f"{lock}.quarantine")

    result = _run_bash(
        _LOCK_PREAMBLE
        + """
before="$(git_lock_identity "$LOCK")"
sleep 1
mv "$LOCK" "$QUARANTINE"
after="$(git_lock_identity "$QUARANTINE")"
[ "$before" = "$after" ]
""",
        env={"LOCK": str(lock), "QUARANTINE": str(quarantine)},
    )

    assert result.returncode == 0, result.stderr
    assert quarantine.exists()


def test_fd_mutex_excludes_a_second_process(tmp_path: Path) -> None:
    mutex = tmp_path / "update.lock"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    env = {
        **os.environ,
        "MUTEX": str(mutex),
        "READY": str(ready),
        "RELEASE": str(release),
    }
    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            _LOCK_PREAMBLE
            + """
exec 9> "$MUTEX"
acquire_fd_mutex 9
touch "$READY"
while [ ! -e "$RELEASE" ]; do sleep 0.05; done
""",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), holder.communicate(timeout=1)[1]

        contender = _run_bash(
            _LOCK_PREAMBLE
            + """
exec 9> "$MUTEX"
acquire_fd_mutex 9
""",
            env=env,
        )
        assert contender.returncode != 0
    finally:
        release.touch()
        _, holder_stderr = holder.communicate(timeout=3)

    assert holder.returncode == 0, holder_stderr


def test_absent_index_lock_continues_without_recovery_mutation(tmp_path: Path) -> None:
    git_dir = tmp_path / "checkout" / ".git"
    git_dir.mkdir(parents=True)

    result = _recover(
        git_dir,
        """
remove_git_index_lock() { printf 'removed\n' > "$TRACE"; }
""",
    )

    assert result.returncode == 0
    assert not (tmp_path / "trace").exists()


def test_unrelated_git_process_does_not_own_index_lock(tmp_path: Path) -> None:
    _, lock = _lock_paths(tmp_path)

    result = _run_bash(
        _LOCK_PREAMBLE
        + """
pgrep() { printf '4321\n'; return 0; }
lsof() { return 1; }
git_lock_owner_state "$LOCK"
""",
        env={"LOCK": str(lock)},
    )

    assert result.returncode == 1
    assert result.stdout == ""


def test_process_with_the_exact_index_lock_open_is_the_owner(tmp_path: Path) -> None:
    _, lock = _lock_paths(tmp_path)

    result = _run_bash(
        _LOCK_PREAMBLE
        + """
pgrep() { return 1; }
lsof() { printf '8765\n'; return 0; }
git_lock_owner_state "$LOCK"
""",
        env={"LOCK": str(lock)},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "8765"


def test_live_owned_index_lock_fails_closed(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path)

    result = _recover(
        git_dir,
        """
git_lock_owner_state() { printf '4321\n'; return 0; }
""",
    )

    assert result.returncode != 0
    assert lock.exists()
    assert "active process" in result.stderr
    assert "4321" in result.stderr


def test_recent_unowned_index_lock_settles_then_recovers(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path, age_seconds=1)

    result = _recover(
        git_dir,
        """
git_lock_owner_state() { return 1; }
sleep() { printf '%s\\n' "$*" > "$TRACE"; }
""",
    )

    assert result.returncode == 0
    assert not lock.exists()
    settle_seconds = int((tmp_path / "trace").read_text().strip())
    assert 1 <= settle_seconds <= 5
    assert "confirm it is orphaned" in result.stderr
    assert "Recovered stale Git index lock" in result.stderr


def test_ambiguous_index_lock_owner_fails_closed(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path)

    result = _recover(
        git_dir,
        """
git_lock_owner_state() { return 2; }
""",
    )

    assert result.returncode != 0
    assert lock.exists()
    assert "could not prove" in result.stderr


def test_proven_stale_index_lock_is_removed(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path)

    result = _recover(
        git_dir,
        """
git_lock_owner_state() { return 1; }
""",
    )

    assert result.returncode == 0
    assert not lock.exists()
    assert "Recovered stale Git index lock" in result.stderr


def test_replaced_index_lock_is_preserved(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path)
    counter = tmp_path / "identity-count"

    result = _recover(
        git_dir,
        """
git_lock_owner_state() { return 1; }
git_lock_identity() {
    count=0
    [ ! -f "$IDENTITY_COUNT" ] || count="$(cat "$IDENTITY_COUNT")"
    count=$((count + 1))
    printf '%s' "$count" > "$IDENTITY_COUNT"
    if [ "$count" = 1 ]; then
        printf '100:%s:0\n' "$OLD_MTIME"
    else
        printf '200:%s:0\n' "$OLD_MTIME"
    fi
}
""",
    )

    assert result.returncode != 0
    assert lock.exists()
    assert int(counter.read_text()) >= 2
    assert "changed during recovery" in result.stderr


def test_stale_index_lock_removal_failure_stops_recovery(tmp_path: Path) -> None:
    git_dir, lock = _lock_paths(tmp_path)

    result = _recover(
        git_dir,
        """
git_lock_owner_state() { return 1; }
remove_git_index_lock() { return 1; }
""",
    )

    assert result.returncode != 0
    assert lock.exists()
    assert "could not remove" in result.stderr


def test_prepare_redeploy_stops_before_pull_for_unrecoverable_lock(
    tmp_path: Path,
) -> None:
    git_dir, lock = _lock_paths(tmp_path, age_seconds=1)
    checkout = git_dir.parent
    trace = tmp_path / "git-trace"
    result = _run_bash(
        _LOCK_PREAMBLE
        + """
git_lock_owner_state() { return 2; }
git() {
    printf '%s\\n' "$*" >> "$TRACE"
    case "$*" in
        *"rev-parse --is-inside-work-tree"*) printf 'true\\n' ;;
        *"rev-parse --absolute-git-dir"*) printf '%s\\n' "$GIT_DIR" ;;
        *"pull --ff-only"*) printf 'PULL\\n' >> "$TRACE" ;;
    esac
}
prepare_redeploy
""",
        env={
            "SELF_CHECKOUT": str(checkout),
            "GIT_DIR": str(git_dir),
            "TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert lock.exists()
    assert "pull --ff-only" not in trace.read_text()


def test_prepare_redeploy_mutex_blocks_before_pull(tmp_path: Path) -> None:
    git_dir = tmp_path / "checkout" / ".git"
    git_dir.mkdir(parents=True)
    trace = tmp_path / "git-trace"
    result = _run_bash(
        _LOCK_PREAMBLE
        + """
acquire_fd_mutex() { return 1; }
git() {
    printf '%s\\n' "$*" >> "$TRACE"
    case "$*" in
        *"rev-parse --is-inside-work-tree"*) printf 'true\\n' ;;
        *"rev-parse --absolute-git-dir"*) printf '%s\\n' "$GIT_DIR" ;;
        *"pull --ff-only"*) printf 'PULL\\n' >> "$TRACE" ;;
    esac
}
prepare_redeploy
""",
        env={
            "SELF_CHECKOUT": str(git_dir.parent),
            "GIT_DIR": str(git_dir),
            "TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert "another Update" in result.stderr
    assert "pull --ff-only" not in trace.read_text()


def _frontend_checkout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    node_modules = frontend / "node_modules"
    dist = frontend / "dist"
    node_modules.mkdir(parents=True)
    dist.mkdir()
    (dist / "index.html").write_text("live build")
    return checkout, node_modules, dist, node_modules / ".corsia-dist-stage"


def _run_build(checkout: Path, trace: Path, body: str = "") -> subprocess.CompletedProcess[str]:
    return _run_bash(
        """
set -e
fail() { printf 'FAIL:%s\n' "$*" >&2; }
warn() { printf 'WARN:%s\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
npm() {
    [ "$1" = run ] || return 0
    printf 'BUILD\n' >> "$TRACE"
    mkdir -p "$CORSIA_BUILD_OUTPUT"
    printf 'new build' > "$CORSIA_BUILD_OUTPUT/index.html"
}
"""
        + body
        + """
redeploy_build "$CHECKOUT"
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )


def test_stage_only_residue_is_discarded_and_rebuilt(tmp_path: Path) -> None:
    checkout, _, dist, stage = _frontend_checkout(tmp_path)
    stage.mkdir()
    (stage / "index.html").write_text("stale stage")
    trace = tmp_path / "trace"

    result = _run_build(checkout, trace)

    assert result.returncode == 0
    assert trace.read_text().splitlines() == ["BUILD"]
    assert (dist / "index.html").read_text() == "live build"
    assert (stage / "index.html").read_text() == "new build"
    assert Path(f"{stage}.prepared").exists()


def test_validated_prepared_stage_is_never_discarded(tmp_path: Path) -> None:
    checkout, _, _, stage = _frontend_checkout(tmp_path)
    stage.mkdir()
    (stage / "index.html").write_text("validated build")
    marker = Path(f"{stage}.prepared")
    marker.touch()
    trace = tmp_path / "trace"

    result = _run_build(checkout, trace)

    assert result.returncode != 0
    assert (stage / "index.html").read_text() == "validated build"
    assert marker.exists()
    assert not trace.exists()
    assert "already awaiting restart" in result.stderr


@pytest.mark.parametrize("recovery_name", [".corsia-dist-previous", ".corsia-dist-previous.absent"])
def test_rollback_backup_or_marker_refuses_stage_cleanup(
    tmp_path: Path, recovery_name: str
) -> None:
    checkout, node_modules, _, stage = _frontend_checkout(tmp_path)
    stage.mkdir()
    (stage / "index.html").write_text("stale stage")
    recovery = node_modules / recovery_name
    if recovery_name.endswith(".absent"):
        recovery.touch()
    else:
        recovery.mkdir()
    trace = tmp_path / "trace"

    result = _run_build(checkout, trace)

    assert result.returncode != 0
    assert stage.exists()
    assert recovery.exists()
    assert not trace.exists()


def test_stage_only_cleanup_failure_stops_before_rebuild(tmp_path: Path) -> None:
    checkout, _, _, stage = _frontend_checkout(tmp_path)
    stage.mkdir()
    trace = tmp_path / "trace"

    result = _run_build(
        checkout,
        trace,
        """
discard_stale_redeploy_stage() { return 1; }
""",
    )

    assert result.returncode != 0
    assert stage.exists()
    assert not trace.exists()
    assert "could not remove stale frontend stage" in result.stderr
