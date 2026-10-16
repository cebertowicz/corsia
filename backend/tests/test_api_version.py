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

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "one", "first: subject")
    monkeypatch.setattr(api_version, "_REPO_ROOT", repo)

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

    monkeypatch.setattr(api_version, "_REPO_ROOT", tmp_path)
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

    build = {"label": "0.4.0", "short_hash": "abc1234", "full_hash": "abc1234" * 5 + "ab"}
    (tmp_path / "build-info.json").write_text(json.dumps(build))
    monkeypatch.setattr(api_version, "FRONTEND_DIR", tmp_path)
    monkeypatch.setattr(api_version, "_APP_BUNDLE", tmp_path / "CORSIA.app")
    monkeypatch.setattr(
        api_version.subprocess,
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

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "one\n", "first")
    tracked = repo / "file.txt"
    later = tracked.stat().st_mtime + 60
    os.utime(tracked, (later, later))
    index = repo / ".git" / "index"
    before = index.stat().st_mtime_ns
    monkeypatch.setattr(api_version, "_REPO_ROOT", repo)

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

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(api_version.subprocess, "check_output", fail)

    assert api_version._git_read(["status", "--porcelain"]) is None
    assert api_version._is_dirty() is None
    assert api_version._current_head_info()["dirty"] is None
    assert api_version._capture_backend_build_info()["dirty"] is None


def test_clean_status_read_is_false_not_unknown(monkeypatch):
    import api.version as api_version

    monkeypatch.setattr(api_version.subprocess, "check_output", lambda *_a, **_k: b"\n")
    assert api_version._is_dirty() is False
    monkeypatch.setattr(api_version.subprocess, "check_output", lambda *_a, **_k: b" M f\n")
    assert api_version._is_dirty() is True


def test_version_payload_carries_the_app_version_and_shell_protocol(monkeypatch):
    """The shell's compatibility check reads ``app``; the computer name is never on this public route."""
    import api.version as api_version

    app = {"version": "0.4.1", "shell_protocol": 1}
    monkeypatch.setattr(api_version, "_read_frontend_build_info", lambda: {"app": app})
    monkeypatch.setattr(api_version, "_current_head_info", lambda: _UNAVAILABLE_HEAD)
    payload = api_version._version_payload()
    assert payload["app"] == app
    assert "name" not in payload["app"]

    monkeypatch.setattr(api_version, "_read_frontend_build_info", api_version._placeholder_frontend_info)
    placeholder = api_version._version_payload()["app"]
    assert isinstance(placeholder["shell_protocol"], int)
    assert placeholder["version"] == "unknown"
