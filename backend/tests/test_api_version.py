"""/api/version reports the exact on-disk HEAD and dirty state on every request."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_UNAVAILABLE_HEAD = {
    "short_hash": "unknown",
    "full_hash": "unknown",
    "commit_iso": "",
    "commit_subject": "(git unavailable)",
    "dirty": None,
}


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` without user signing or hooks and return stdout."""
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, content: str, subject: str) -> None:
    (repo / "file.txt").write_text(content)
    _git(repo, "add", "file.txt")
    _git(
        repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", subject,
    )


@pytest.mark.asyncio
async def test_version_reports_exact_on_disk_head_and_dirty_state(tmp_path, monkeypatch):
    import api.version as api_version
    from infra import build_info

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "one", "first: subject")
    monkeypatch.setattr(build_info, "_REPO_ROOT", repo)

    clean = await api_version.get_version()
    assert clean["backend"]["current_head"] == {
        "short_hash": _git(repo, "rev-parse", "--short=7", "HEAD"),
        "full_hash": _git(repo, "rev-parse", "HEAD"),
        "commit_iso": _git(repo, "log", "-1", "--format=%cI", "HEAD"),
        "commit_subject": "first: subject",
        "dirty": False,
    }
    assert clean["backend"]["full_hash"] == api_version._BACKEND_BUILD["full_hash"]
    assert clean["backend"]["start_iso"] == api_version._BACKEND_BUILD["start_iso"]

    (repo / "file.txt").write_text("edited")
    assert (await api_version.get_version())["backend"]["current_head"]["dirty"] is True

    _commit(repo, "two", "second: subject")
    advanced = await api_version.get_version()
    assert advanced["backend"]["current_head"]["full_hash"] == _git(repo, "rev-parse", "HEAD")
    assert advanced["backend"]["current_head"]["commit_subject"] == "second: subject"
    assert advanced["backend"]["current_head"]["dirty"] is False
    assert advanced["backend"]["full_hash"] == clean["backend"]["full_hash"]


@pytest.mark.asyncio
async def test_version_reports_unavailable_head_outside_a_repository(tmp_path, monkeypatch):
    import api.version as api_version
    from infra import build_info

    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    result = await api_version.get_version()

    assert result["backend"]["current_head"] == _UNAVAILABLE_HEAD


@pytest.mark.asyncio
async def test_app_bundle_reports_bundled_build_identity_and_service_generation(
    tmp_path, monkeypatch
):
    """An app bundle has no git, so its backend identity is the bundled build-info."""
    import json

    import api.version as api_version
    from infra import build_info

    build = {"label": "0.4.0", "short_hash": "abc1234", "full_hash": "abc1234" * 5 + "ab"}
    (tmp_path / "build-info.json").write_text(json.dumps(build))
    monkeypatch.setattr(build_info, "FRONTEND_DIR", tmp_path)
    monkeypatch.setattr(build_info, "app_bundle_root", lambda: tmp_path / "CORSIA.app")
    monkeypatch.setattr(
        build_info.subprocess,
        "check_output",
        lambda *_a, **_k: pytest.fail("app bundle ran git"),
    )
    monkeypatch.setattr(api_version, "_BACKEND_BUILD", api_version._capture_backend_build_info())
    monkeypatch.setenv("CORSIA_SERVICE_GENERATION", "0.4.0+7")

    backend = (await api_version.get_version())["backend"]

    assert backend["full_hash"] == build["full_hash"]
    assert backend["current_head"] is None
    assert backend["generation"] == "0.4.0+7"


def test_version_git_reads_never_rewrite_the_serving_index(tmp_path, monkeypatch):
    """A version poll during Update must not take index.lock in the serving checkout."""
    import os

    import api.version as api_version
    from infra import build_info

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "one\n", "first")
    tracked = repo / "file.txt"
    later = tracked.stat().st_mtime + 60
    os.utime(tracked, (later, later))
    index = repo / ".git" / "index"
    before = index.stat().st_mtime_ns
    monkeypatch.setattr(build_info, "_REPO_ROOT", repo)

    assert api_version._git(["status", "--porcelain"]) == ""
    assert index.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="git", timeout=2),
        subprocess.CalledProcessError(128, "git"),
        FileNotFoundError("git"),
    ],
    ids=["timeout", "nonzero-exit", "git-missing"],
)
def test_failed_status_read_is_unknown_never_clean(monkeypatch, failure):
    """A git status that cannot answer reports dirty as None, not False."""
    import api.version as api_version
    from infra import build_info

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(build_info.subprocess, "check_output", fail)

    assert build_info._git_read(["status", "--porcelain"]) is None
    assert api_version._is_dirty() is None
    assert api_version._current_head_info()["dirty"] is None
    assert api_version._capture_backend_build_info()["dirty"] is None


def test_clean_status_read_is_false_not_unknown(monkeypatch):
    import api.version as api_version
    from infra import build_info

    monkeypatch.setattr(build_info.subprocess, "check_output", lambda *_a, **_k: b"\n")
    assert api_version._is_dirty() is False
    monkeypatch.setattr(build_info.subprocess, "check_output", lambda *_a, **_k: b" M f\n")
    assert api_version._is_dirty() is True


def test_version_payload_carries_the_bundle_stamp_and_this_backends_shell_protocol(monkeypatch):
    """The shell's compatibility check reads ``app``; the computer name is never on this public route."""
    import api.version as api_version
    from infra import build_info

    stamped = {"version": "0.4.1-dev.219", "shell_protocol": 99}
    monkeypatch.setattr(build_info, "frontend_build_info", lambda: {"app": stamped})
    monkeypatch.setattr(api_version, "_current_head_info", lambda: _UNAVAILABLE_HEAD)
    monkeypatch.setattr(build_info, "_git_read", lambda _args: pytest.fail("a stamped build never asks git"))

    app = api_version._version_payload()["app"]

    assert app == {"version": "0.4.1-dev.219", "shell_protocol": api_version.SHELL_PROTOCOL}
    assert "name" not in app


def test_app_bundle_without_a_stamp_reports_no_version_and_never_runs_git(monkeypatch, tmp_path):
    from infra import build_info

    monkeypatch.setattr(build_info, "app_bundle_root", lambda: tmp_path / "CORSIA.app")
    monkeypatch.setattr(build_info, "_git_read", lambda _args: pytest.fail("an app bundle ran git"))

    assert build_info.app_version({}) is None
    assert build_info.app_version({"app": {"version": None}}) is None
    assert build_info.app_version(build_info.placeholder_frontend_info()) is None


def test_source_checkout_reports_the_release_tag_git_describes_without_its_v(monkeypatch):
    from infra import build_info

    asked = []

    def describe(args):
        asked.append(args)
        return "v0.4.1-230-g1a2b3c4"

    monkeypatch.setattr(build_info, "app_bundle_root", lambda: None)
    monkeypatch.setattr(build_info, "_git_read", describe)

    assert build_info.app_version(build_info.placeholder_frontend_info()) == "0.4.1-230-g1a2b3c4"
    assert asked == [["describe", "--tags", "--match", "v[0-9]*"]]


@pytest.mark.parametrize("failure", [None, ""])
def test_source_checkout_reports_no_version_when_git_cannot_describe(monkeypatch, failure):
    from infra import build_info

    monkeypatch.setattr(build_info, "app_bundle_root", lambda: None)
    monkeypatch.setattr(build_info, "_git_read", lambda _args: failure)

    assert build_info.app_version({}) is None


def test_source_checkout_prefers_a_stamp_over_git(monkeypatch):
    from infra import build_info

    monkeypatch.setattr(build_info, "app_bundle_root", lambda: None)
    monkeypatch.setattr(build_info, "_git_read", lambda _args: pytest.fail("a stamped build never asks git"))

    assert build_info.app_version({"app": {"version": "0.4.1-dev.230"}}) == "0.4.1-dev.230"


def test_the_bundle_build_stamps_its_release_version_into_the_frontend_build_info():
    """The shell's side of the comparison is `package_info().version`; this is the host's side of the same definition."""
    repo = Path(__file__).resolve().parents[2]
    bundle = (repo / "desktop/scripts/build-macos-bundle.sh").read_text()
    generator = (repo / "frontend/scripts/generate-build-info.mjs").read_text()

    build_line = next(line for line in bundle.splitlines() if "npm run build" in line)
    assert 'CORSIA_APP_VERSION="$app_version"' in build_line
    assert "version: process.env.CORSIA_APP_VERSION" in generator
    assert "package.json" not in generator
