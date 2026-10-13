from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from service_script_fixtures import run_bash as _run_bash



def test_redeploy_refuses_existing_prepared_release_before_pull(tmp_path: Path) -> None:
    trace = tmp_path / "trace.log"
    git_dir = tmp_path / "git-dir"
    checkout = tmp_path / "serving-checkout"
    git_dir.mkdir()
    checkout.mkdir()
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL %s\\n' "$*" >> "$TRACE"; }
PYTHON=.venv/bin/python
source scripts/corsia.d/runtime.sh
git() {
    case "$*" in
        *"symbolic-ref"*) echo dev ;;
        *"status --porcelain"*)
            mkdir -p "$CHECKOUT/frontend/node_modules/.corsia-dist-stage"
            : > "$CHECKOUT/frontend/node_modules/.corsia-dist-stage.prepared"
            ;;
        *"pull --ff-only"*) printf 'STEP:pull\\n' >> "$TRACE" ;;
    esac
}
prepare_redeploy_locked "$CHECKOUT" "$GIT_DIR"
""",
        env={
            "CHECKOUT": str(checkout),
            "GIT_DIR": str(git_dir),
            "TRACE": str(trace),
        },
    )

    assert result.returncode == 41
    assert "STEP:pull" not in trace.read_text()


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("git pull", 41),
        ("redeploy_sync_python_dependencies", 42),
        ("redeploy_build", 42),
    ],
)
def test_prepare_redeploy_preserves_fetch_and_build_stage_statuses(
    tmp_path: Path, failure: str, expected_status: int,
) -> None:
    """The Python caller receives a code only when the failed stage is known."""
    checkout = tmp_path / "serving-checkout"
    git_dir = tmp_path / "git-dir"
    checkout.mkdir()
    git_dir.mkdir()
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
PYTHON=.venv/bin/python
source scripts/corsia.d/runtime.sh
recover_stale_git_index_lock() { return 0; }
git() {
    case "$*" in
        *"symbolic-ref"*) echo dev ;;
        *"status --porcelain"*) ;;
        *"pull --ff-only"*) [ "$FAILURE" = "git pull" ] && return 1 ;;
    esac
    return 0
}
redeploy_sync_python_dependencies() { [ "$FAILURE" = "redeploy_sync_python_dependencies" ] && return 1; return 0; }
redeploy_build() { [ "$FAILURE" = "redeploy_build" ] && return 1; return 0; }
prepare_redeploy_locked "$CHECKOUT" "$GIT_DIR"
""",
        env={"CHECKOUT": str(checkout), "GIT_DIR": str(git_dir), "FAILURE": failure},
    )

    assert result.returncode == expected_status


def _write_frontend_build(frontend_dir: Path, sha: str, body: str) -> None:
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text(body)
    (frontend_dir / "build-info.json").write_text(
        json.dumps({"full_hash": sha, "dirty": False})
    )


def _run_startup_release(
    checkout: Path,
    git_dir: Path,
    trace: Path,
    *,
    head_sha: str,
    before_ensure: str = "",
    build: str = "printf 'UNEXPECTED_BUILD\\n' >> \"$TRACE\"; return 1",
    ownership: str = """
service_installed() { return 1; }
service_is_loaded() { return 1; }
is_backend_running() { return 1; }
""",
):
    return _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
BACKEND_PORT=9999
PYTHON=.venv/bin/python
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
"""
        + ownership
        + """
acquire_fd_mutex() { printf 'MUTEX\\n' >> "$TRACE"; }
git() {
    case "$*" in
        *"rev-parse --is-inside-work-tree"*) echo true ;;
        *"rev-parse --absolute-git-dir"*) echo "$GIT_DIR" ;;
        *"rev-parse HEAD"*) echo "$HEAD_SHA" ;;
    esac
}
redeploy_build() {
"""
        + build
        + """
}
"""
        + before_ensure
        + """
ensure_current_frontend_release
""",
        env={
            "CHECKOUT": str(checkout),
            "GIT_DIR": str(git_dir),
            "HEAD_SHA": head_sha,
            "TRACE": str(trace),
        },
    )


def test_startup_rebuilds_backend_only_sha_mismatch_despite_newer_dist(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    (frontend / "node_modules").mkdir(parents=True)
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    os.utime(frontend / "dist" / "index.html", (2_000_000_000, 2_000_000_000))
    trace = tmp_path / "trace.log"

    result = _run_startup_release(
        checkout,
        git_dir,
        trace,
        head_sha="new-sha",
        build="""
    local stage="$1/frontend/node_modules/.corsia-dist-stage"
    mkdir -p "$stage"
    printf '%s' 'new build' > "$stage/index.html"
    printf '{"full_hash":"%s","dirty":false}' "$HEAD_SHA" > "$stage/build-info.json"
    : > "${stage}.prepared"
    printf 'BUILD\\n' >> "$TRACE"
""",
    )

    assert result.returncode == 0, result.stderr
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not (frontend / "node_modules" / ".corsia-dist-previous").exists()
    assert trace.read_text().splitlines() == ["MUTEX", "BUILD"]


def test_startup_consumes_valid_exact_prepared_stage_under_update_mutex(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    _write_frontend_build(stage, "new-sha", "new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_startup_release(
        checkout, git_dir, trace, head_sha="new-sha",
    )

    assert result.returncode == 0, result.stderr
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not stage.exists()
    assert not Path(f"{stage}.prepared").exists()
    assert not (frontend / "node_modules" / ".corsia-dist-previous").exists()
    assert trace.read_text().splitlines() == ["MUTEX"]


def test_replacement_startup_finishes_exact_completed_activation(
    tmp_path: Path,
) -> None:
    """Lifespan accepts the exact dist installed just before bootstrap."""
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    backup = frontend / "node_modules" / ".corsia-dist-previous"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    _write_frontend_build(stage, "new-sha", "new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_startup_release(
        checkout,
        git_dir,
        trace,
        head_sha="new-sha",
        before_ensure='activate_prepared_frontend "$CHECKOUT"\n',
    )

    assert result.returncode == 0, result.stderr
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not stage.exists()
    assert not Path(f"{stage}.prepared").exists()
    assert not backup.exists()
    assert trace.read_text().splitlines() == ["MUTEX"]


def test_startup_refuses_and_retains_prepared_stage_from_another_sha(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    _write_frontend_build(stage, "other-sha", "prepared build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_startup_release(
        checkout, git_dir, trace, head_sha="new-sha",
    )

    assert result.returncode != 0
    assert (frontend / "dist" / "index.html").read_text() == "old build"
    assert (stage / "index.html").read_text() == "prepared build"
    assert Path(f"{stage}.prepared").exists()
    assert trace.read_text().splitlines()[0] == "MUTEX"
    assert "does not match checkout new-sha" in trace.read_text()


def test_startup_foreign_service_ownership_has_zero_asset_effects(
    tmp_path: Path,
) -> None:
    """A plist-launched backend proves its definition before touching assets.

    The generation stamp is what launchd puts in the environment, so its
    presence is what says this backend was started by an installed definition
    and therefore has one to verify against.
    """
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    trace = tmp_path / "trace.log"
    trace.touch()

    result = _run_startup_release(
        checkout,
        git_dir,
        trace,
        head_sha="new-sha",
        ownership="""
CORSIA_SERVICE_GENERATION=1700000000-901-4242
service_installed() { return 0; }
service_is_loaded() { return 0; }
require_owned_service_definition() {
    printf 'OWNER_REFUSED\\n' >> "$TRACE"
    return 1
}
""",
    )

    assert result.returncode != 0
    assert (frontend / "dist" / "index.html").read_text() == "old build"
    assert trace.read_text().splitlines() == ["OWNER_REFUSED"]


def test_startup_without_a_generation_ignores_another_checkouts_service(
    tmp_path: Path,
) -> None:
    """A backend no plist launched has no installed definition to answer for.

    A worktree instance starts with the machine's production plist installed
    and loaded. Treating that as its own owner refused every worktree startup
    and forced a test-mode bypass; the generation stamp is absent here, so the
    foreign definition is simply not this backend's business.
    """
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    (frontend / "node_modules").mkdir(parents=True)
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    trace = tmp_path / "trace.log"

    result = _run_startup_release(
        checkout,
        git_dir,
        trace,
        head_sha="new-sha",
        ownership="""
unset CORSIA_SERVICE_GENERATION
service_installed() { return 0; }
service_is_loaded() { return 0; }
require_owned_service_definition() {
    printf 'OWNER_CHECKED\\n' >> "$TRACE"
    return 1
}
""",
        build="""
    local stage="$1/frontend/node_modules/.corsia-dist-stage"
    mkdir -p "$stage"
    printf '%s' 'new build' > "$stage/index.html"
    printf '{"full_hash":"%s","dirty":false}' "$HEAD_SHA" > "$stage/build-info.json"
    : > "${stage}.prepared"
    printf 'BUILD\\n' >> "$TRACE"
""",
    )

    assert result.returncode == 0, result.stderr
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert "OWNER_CHECKED" not in trace.read_text()


def test_startup_keeps_assets_while_owned_old_backend_is_healthy(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    _write_frontend_build(frontend / "dist", "old-sha", "old build")
    _write_frontend_build(stage, "new-sha", "new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_startup_release(
        checkout,
        git_dir,
        trace,
        head_sha="new-sha",
        ownership="""
service_installed() { return 0; }
service_is_loaded() { return 0; }
require_owned_service_definition() { return 0; }
is_backend_running() { return 0; }
""",
    )

    assert result.returncode != 0
    assert (frontend / "dist" / "index.html").read_text() == "old build"
    assert (stage / "index.html").read_text() == "new build"
    assert Path(f"{stage}.prepared").exists()
    assert "another backend is still healthy" in trace.read_text()


@pytest.mark.parametrize(
    ("dirty", "write_index"),
    [(True, True), (False, False)],
    ids=["dirty-metadata", "missing-entrypoint"],
)
def test_frontend_release_match_rejects_unusable_or_dirty_build(
    tmp_path: Path,
    dirty: bool,
    write_index: bool,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    if write_index:
        (release / "index.html").write_text("release")
    (release / "build-info.json").write_text(
        json.dumps({"full_hash": "exact-sha", "dirty": dirty})
    )

    result = _run_bash(
        """
PYTHON=.venv/bin/python
source scripts/corsia.d/runtime.sh
frontend_release_matches_sha "$RELEASE" exact-sha
""",
        env={"RELEASE": str(release)},
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("had_dist", "failed_move"),
    [(True, 2), (False, 1)],
    ids=["backup-window", "prior-absence-window"],
)
def test_startup_completes_an_interrupted_frontend_activation(
    tmp_path: Path,
    had_dist: bool,
    failed_move: int,
) -> None:
    """Startup resumes only the exact second move owned by activation."""
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    backup = frontend / "node_modules" / ".corsia-dist-previous"
    absent = Path(f"{backup}.absent")
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    if had_dist:
        _write_frontend_build(frontend / "dist", "old-sha", "old build")
    _write_frontend_build(stage, "new-sha", "new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"
    interrupted = f"""
move_count=0
mv() {{
    move_count=$((move_count + 1))
    [ "$move_count" -ne {failed_move} ] || return 1
    command mv "$@"
}}
activate_prepared_frontend "$CHECKOUT" || :
unset -f mv
"""

    result = _run_startup_release(
        checkout, git_dir, trace, head_sha="new-sha", before_ensure=interrupted,
    )

    assert result.returncode == 0, trace.read_text()
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not any((stage.exists(), Path(f"{stage}.prepared").exists(),
                    backup.exists(), absent.exists()))


@pytest.mark.parametrize("state", ["foreign", "missing", "dirty", "inconsistent"])
def test_startup_preserves_ambiguous_interrupted_activation(
    tmp_path: Path,
    state: str,
) -> None:
    """Only exact, complete rollback evidence permits activation recovery."""
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    backup = frontend / "node_modules" / ".corsia-dist-previous"
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    _write_frontend_build(backup, "old-sha", "old build")
    if state != "missing":
        _write_frontend_build(
            stage, "other-sha" if state == "foreign" else "new-sha", "new build",
        )
    if state == "dirty":
        (stage / "build-info.json").write_text(
            json.dumps({"full_hash": "new-sha", "dirty": True})
        )
    Path(f"{stage}.prepared").touch()
    if state == "inconsistent":
        Path(f"{backup}.absent").touch()
    before = {
        str(item.relative_to(frontend)): item.read_bytes()
        for item in frontend.rglob("*") if item.is_file()
    }
    trace = tmp_path / "trace.log"

    result = _run_startup_release(checkout, git_dir, trace, head_sha="new-sha")

    after = {
        str(item.relative_to(frontend)): item.read_bytes()
        for item in frontend.rglob("*") if item.is_file()
    }
    assert result.returncode != 0
    assert after == before
    assert not (frontend / "dist").exists()
