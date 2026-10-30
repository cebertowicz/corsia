"""Backend Git never runs a program named by a repository's own config.

Every case plants a marker-touching command in the repository's local config on
real Git repositories, first proves plain Git runs it, then proves the backend's
runners do not. The person's own global config must keep applying.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from infra.git_local_config_guard import (
    ConfigEntry,
    GitGuard,
    _submodule_argv,
    build_guard,
    guard_for,
    guarded_run,
)
from infra.project_repo_policy import RepoPolicy, repo_checkout_state
from tools.worktrees import WorktreeCreateTool

_IDENT = ["-c", "user.name=T", "-c", "user.email=t@corsia.local"]


@pytest.fixture(autouse=True)
def person_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate Git from the machine's config; return the person's global file."""
    for name in (
        "GIT_SSH_COMMAND", "GIT_SSH", "GIT_PROXY_COMMAND", "GIT_ASKPASS",
        "GIT_ALLOW_PROTOCOL", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "person.gitconfig"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    return path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "init.defaultRefFormat=files", *args],
        cwd=str(repo), check=check, capture_output=True, text=True,
    )


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@corsia.local")
    _git(repo, "commit", "--allow-empty", "-m", "first")
    return repo


def _touch(tmp_path: Path, name: str) -> tuple[Path, str]:
    """Return a marker path and a shell command that creates it."""
    marker = tmp_path / f"marker-{name}"
    return marker, f"touch {marker}"


def _script(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Return a marker path and an executable that creates it and fails."""
    marker = tmp_path / f"marker-{name}"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    return marker, script


def _ran(marker: Path) -> bool:
    ran = marker.exists()
    marker.unlink(missing_ok=True)
    return ran


def _assert_guarded(repo: Path, marker: Path, argv: list[str]) -> None:
    """Plain Git runs the planted command; the guarded runner does not."""
    _git(repo, *argv, check=False)
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    guarded_run(repo, argv, timeout=60)
    assert not _ran(marker)


def _clone_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A repo on branch dev cloned from a bare remote that has a newer commit."""
    author = _repo(tmp_path, "author")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(author), str(remote))
    repo = tmp_path / "work"
    _git(tmp_path, "clone", "--branch", "dev", str(remote), str(repo))
    _git(author, "remote", "add", "origin", str(remote))
    _git(author, *_IDENT, "commit", "--allow-empty", "-m", "second")
    _git(author, "push", "origin", "dev")
    return repo, remote


def test_status_does_not_run_a_local_fsmonitor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "fsmonitor")
    _git(repo, "config", "core.fsmonitor", f"{command}; echo")
    _git(repo, "status")
    assert _ran(marker)
    state = repo_checkout_state(repo)
    assert state.branch == "dev"
    assert not _ran(marker)


def test_a_hook_in_the_repository_is_not_run_by_a_guarded_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    marker = tmp_path / "marker-hook"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "commit", "--allow-empty", "-m", "plain")
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    done = guarded_run(repo, ["commit", "--allow-empty", "-m", "guarded"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)
    assert len(_git(repo, "log", "--oneline").stdout.splitlines()) == 3  # still commits


def test_a_local_hooks_path_is_not_used_by_a_worktree_add(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "marker-checkout"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "worktree", "add", str(tmp_path / "plain"), "HEAD")
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    done = guarded_run(repo, ["worktree", "add", str(tmp_path / "guarded"), "HEAD"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)
    assert (tmp_path / "guarded" / ".git").exists()


def test_local_clean_filter_is_not_run_and_a_required_one_does_not_fail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "clean")
    (repo / ".gitattributes").write_text("*.dat filter=x\n", encoding="utf-8")
    (repo / "a.dat").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *_IDENT, "commit", "-m", "add")
    (repo / "a.dat").write_text("two\n", encoding="utf-8")
    _git(repo, "config", "filter.x.clean", f"{command}; cat")
    _git(repo, "status")
    assert _ran(marker)
    assert repo_checkout_state(repo).clean is False
    assert not _ran(marker)
    _git(repo, "config", "filter.x.required", "true")
    assert repo_checkout_state(repo).clean is False  # no fatal error
    assert not _ran(marker)


def test_local_smudge_filter_is_not_run_when_a_worktree_is_added(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "smudge")
    (repo / ".gitattributes").write_text("*.dat filter=x\n", encoding="utf-8")
    (repo / "a.dat").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *_IDENT, "commit", "-m", "add")
    _git(repo, "config", "filter.x.smudge", f"{command}; cat")
    _git(repo, "worktree", "add", str(tmp_path / "plain"), "HEAD")
    assert _ran(marker)
    guarded_run(repo, ["worktree", "add", str(tmp_path / "guarded"), "HEAD"], timeout=60)
    assert not _ran(marker)
    assert (tmp_path / "guarded" / "a.dat").read_text(encoding="utf-8") == "one\n"


def test_filter_named_with_an_equals_sign_is_still_overridden(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "odd")
    (repo / ".gitattributes").write_text("*.dat filter=a=b\n", encoding="utf-8")
    (repo / "a.dat").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, *_IDENT, "commit", "-m", "add")
    (repo / "a.dat").write_text("two\n", encoding="utf-8")
    _git(repo, "config", "filter.a=b.clean", f"{command}; cat")
    guard = guard_for(repo)
    assert "GIT_CONFIG_KEY_0" in (guard.environment() or {})
    _assert_guarded(repo, marker, ["status", "--porcelain"])


def test_local_ssh_command_is_not_run_by_the_worktree_fetch(tmp_path: Path, monkeypatch) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    marker, command = _touch(tmp_path, "ssh")
    _git(repo, "remote", "set-url", "origin", "ssh://git@example.invalid/x")
    _git(repo, "config", "core.sshCommand", f"{command}; false")
    _git(repo, "fetch", "origin", check=False)
    assert _ran(marker)
    policy = RepoPolicy("p1", "app", repo.resolve(), "dev", True)
    monkeypatch.setattr("tools.worktrees.list_registered_repo_policies", lambda **_k: [policy])
    monkeypatch.setattr("tools.worktrees.emit_product_event", lambda *_a, **_k: None)
    result = _create(repo)
    assert result.startswith("Error:")
    assert not _ran(marker)


def _create(repo: Path) -> str:
    import asyncio

    return asyncio.run(
        WorktreeCreateTool().execute(
            repo_name="app", branch="fix/x", descriptor="x", remote="origin",
            base_branch="dev", _thread_id="thread-1", _thread_project_id="p1",
        )
    )


def test_local_uploadpack_is_not_run_and_the_worktree_is_still_created(
    tmp_path: Path, monkeypatch,
) -> None:
    repo, remote = _clone_with_remote(tmp_path)
    marker, command = _touch(tmp_path, "uploadpack")
    _git(repo, "config", "remote.origin.uploadpack", f"{command}; git-upload-pack")
    _git(repo, "fetch", "origin")
    assert _ran(marker)
    policy = RepoPolicy("p1", "app", repo.resolve(), "dev", True)
    monkeypatch.setattr("tools.worktrees.list_registered_repo_policies", lambda **_k: [policy])
    monkeypatch.setattr("tools.worktrees.emit_product_event", lambda *_a, **_k: None)
    receipt = json.loads(_create(repo))
    assert receipt["created"] is True
    assert not _ran(marker)


def test_person_uploadpack_still_wins_over_a_local_one(tmp_path: Path, person_config: Path) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    mine, mine_cmd = _touch(tmp_path, "mine")
    theirs, theirs_cmd = _touch(tmp_path, "theirs")
    person_config.write_text(
        f'[remote "origin"]\n\tuploadpack = "{mine_cmd}; git-upload-pack"\n', encoding="utf-8",
    )
    _git(repo, "config", "remote.origin.uploadpack", f"{theirs_cmd}; git-upload-pack")
    done = guarded_run(repo, ["fetch", "origin"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert _ran(mine)
    assert not _ran(theirs)


@pytest.mark.parametrize("argv", [
    ["fetch"], ["fetch", "origin"], ["fetch", "origin", "dev"], ["fetch", "--", "origin"],
])
def test_local_uploadpack_is_not_run_wherever_the_remote_is_named(
    tmp_path: Path, argv: list[str],
) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    marker, command = _touch(tmp_path, "uploadpack")
    _git(repo, "config", "remote.origin.uploadpack", f"{command}; git-upload-pack")
    _assert_guarded(repo, marker, argv)


def test_local_uploadpack_is_not_run_by_the_fetch_of_a_missing_object(tmp_path: Path) -> None:
    origin = _repo(tmp_path, "origin")
    (origin / "f").write_text("1\n", encoding="utf-8")
    _git(origin, "add", "f")
    _git(origin, *_IDENT, "commit", "-m", "file")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    repo = tmp_path / "partial"
    _git(tmp_path, "clone", "--filter=blob:none", "--no-checkout", f"file://{origin}", str(repo))
    marker, command = _touch(tmp_path, "lazy")
    _git(repo, "config", "remote.origin.uploadpack", f"{command}; git-upload-pack")
    _assert_guarded(repo, marker, ["cat-file", "-p", "HEAD:f"])


def test_local_alternate_refs_command_is_not_run_and_the_fetch_works(tmp_path: Path) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    other = tmp_path / "alternate.git"
    _git(tmp_path, "init", "--bare", str(other))
    (repo / ".git/objects/info/alternates").write_text(f"{other}/objects\n", encoding="utf-8")
    marker, command = _touch(tmp_path, "alternate-refs")
    _git(repo, "config", "core.alternateRefsCommand", command)
    _assert_guarded(repo, marker, ["fetch", "origin"])
    assert guarded_run(repo, ["fetch", "origin"], timeout=60).returncode == 0


def test_local_alias_is_not_run_as_a_remote_helper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "alias-helper")
    _git(repo, "config", "remote.origin.url", "planted::anything")
    _git(repo, "config", "alias.remote-planted", f"!{command}")
    _assert_guarded(repo, marker, ["fetch", "origin"])


def test_a_remote_helper_named_by_path_in_the_repository_is_not_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, _script_path = _script(tmp_path, "helper-path")
    helper = repo / "git-remote-planted" / "helper"
    helper.parent.mkdir()
    helper.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    _git(repo, "config", "remote.origin.url", "https://example.invalid/x.git")
    _git(repo, "config", "remote.origin.vcs", "planted/helper")
    _assert_guarded(repo, marker, ["fetch", "origin"])


def test_a_helper_on_the_path_is_left_to_the_repository() -> None:
    entries = [ConfigEntry("local", "remote.origin.vcs", "hg")]
    assert not [k for k, _v in build_guard(entries).config if k == "remote.origin.vcs"]


def test_person_ssh_command_still_applies_when_nothing_is_local(
    tmp_path: Path, person_config: Path,
) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    marker, command = _touch(tmp_path, "person-ssh")
    person_config.write_text(f'[core]\n\tsshCommand = "{command}; false"\n', encoding="utf-8")
    _git(repo, "remote", "set-url", "origin", "ssh://git@example.invalid/x")
    guarded_run(repo, ["fetch", "origin"], timeout=60)
    assert _ran(marker)


def test_person_ssh_command_replaces_a_local_one(tmp_path: Path, person_config: Path) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    mine, mine_cmd = _touch(tmp_path, "person-ssh")
    theirs, theirs_cmd = _touch(tmp_path, "local-ssh")
    person_config.write_text(f'[core]\n\tsshCommand = "{mine_cmd}; false"\n', encoding="utf-8")
    _git(repo, "remote", "set-url", "origin", "ssh://git@example.invalid/x")
    _git(repo, "config", "core.sshCommand", f"{theirs_cmd}; false")
    guarded_run(repo, ["fetch", "origin"], timeout=60)
    assert _ran(mine)
    assert not _ran(theirs)


def test_local_git_proxy_is_not_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, script = _script(tmp_path, "proxy")
    _git(repo, "config", "core.gitProxy", str(script))
    _assert_guarded(repo, marker, ["ls-remote", "git://example.invalid/x"])


def test_person_git_proxy_still_applies_over_a_local_one(
    tmp_path: Path, person_config: Path,
) -> None:
    repo = _repo(tmp_path)
    mine, mine_script = _script(tmp_path, "mine-proxy")
    theirs, theirs_script = _script(tmp_path, "theirs-proxy")
    person_config.write_text(f"[core]\n\tgitProxy = {mine_script}\n", encoding="utf-8")
    _git(repo, "config", "core.gitProxy", str(theirs_script))
    guarded_run(repo, ["ls-remote", "git://example.invalid/x"], timeout=60)
    assert _ran(mine)
    assert not _ran(theirs)


def test_local_ext_transport_is_not_enabled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, script = _script(tmp_path, "ext")
    _git(repo, "config", "protocol.ext.allow", "always")
    _git(repo, "remote", "add", "origin", f"ext::{script} %S x")
    _assert_guarded(repo, marker, ["fetch", "origin"])


def test_person_may_still_enable_the_ext_transport(tmp_path: Path, person_config: Path) -> None:
    repo = _repo(tmp_path)
    marker, script = _script(tmp_path, "person-ext")
    person_config.write_text("[protocol \"ext\"]\n\tallow = always\n", encoding="utf-8")
    _git(repo, "remote", "add", "origin", f"ext::{script} %S x")
    guarded_run(repo, ["fetch", "origin"], timeout=60)
    assert _ran(marker)


def test_local_gc_hook_is_not_run_by_the_fetch_housekeeping(tmp_path: Path) -> None:
    repo, _remote = _clone_with_remote(tmp_path)
    marker, command = _touch(tmp_path, "gc")
    shard, number = 0, 0
    while shard < 3:  # loose objects in one fan-out directory trigger automatic gc
        data = f"junk{number}"
        number += 1
        digest = hashlib.sha1(f"blob {len(data)}\0{data}".encode()).hexdigest()
        if digest.startswith("17"):
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=data,
                text=True, capture_output=True, check=True,
            )
            shard += 1
    for key, value in (
        ("gc.auto", "1"), ("gc.autoDetach", "false"), ("maintenance.autoDetach", "false"),
        ("maintenance.strategy", "gc"),  # newer Git's default strategy never runs gc
        ("gc.pruneExpire", "now"), ("gc.recentObjectsHook", f"{command}; true"),
    ):
        _git(repo, "config", key, value)
    _assert_guarded(repo, marker, ["fetch", "origin"])


def _serve_401():
    """A local http server that demands basic auth; returns (server, url)."""
    import http.server
    import threading

    class Unauthorized(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/x.git"


def test_a_local_credential_helper_is_not_run(tmp_path: Path) -> None:
    server, url = _serve_401()
    try:
        repo = _repo(tmp_path)
        marker, script = _script(tmp_path, "helper")
        _git(repo, "config", "credential.helper", f"!{script}")
        _git(repo, "remote", "add", "origin", url)
        _assert_guarded(repo, marker, ["ls-remote", "origin"])
    finally:
        server.shutdown()


def test_a_local_askpass_is_not_used_and_the_persons_own_still_is(
    tmp_path: Path, monkeypatch,
) -> None:
    """The neutral askpass must not silence the person's own prompt helper."""
    server, url = _serve_401()
    try:
        repo = _repo(tmp_path)
        theirs, theirs_script = _script(tmp_path, "askpass-local")
        mine, mine_script = _script(tmp_path, "askpass-person")
        for name in ("GIT_ASKPASS", "SSH_ASKPASS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
        _git(repo, "remote", "add", "origin", url)
        _git(repo, "config", "core.askPass", str(theirs_script))
        _git(repo, "fetch", "--", "origin", check=False)
        assert _ran(theirs), "the plant must work against plain Git or the test proves nothing"
        monkeypatch.setenv("GIT_ASKPASS", str(mine_script))
        done = guarded_run(repo, ["fetch", "--", "origin"], timeout=60)
        assert done.returncode != 0
        assert _ran(mine)
        assert not _ran(theirs)
    finally:
        server.shutdown()


def test_local_fetch_does_not_recurse_into_a_submodule_with_its_own_config(
    tmp_path: Path, person_config: Path,
) -> None:
    person_config.write_text('[protocol "file"]\n\tallow = always\n', encoding="utf-8")
    sub_source = _repo(tmp_path, "sub-source")
    top = _repo(tmp_path, "top")
    _git(top, "submodule", "add", str(sub_source), "sm")
    _git(top, *_IDENT, "commit", "-m", "submodule")
    upstream = tmp_path / "top-upstream"
    subprocess.run(["cp", "-R", str(top), str(upstream)], check=True)
    _git(top, "remote", "add", "origin", str(upstream))
    marker, command = _touch(tmp_path, "submodule-ssh")
    sub = top / "sm"
    _git(sub, "remote", "set-url", "origin", "ssh://git@example.invalid/x")
    _git(sub, "config", "core.sshCommand", f"{command}; false")
    _git(top, "fetch", "--recurse-submodules=yes", "origin", check=False)
    assert _ran(marker)
    done = guarded_run(top, ["fetch", "origin"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)


def _entry(scope: str, key: str, value: str | None) -> ConfigEntry:
    return ConfigEntry(scope, key, value)


def test_credential_helper_list_is_reset_and_the_persons_helpers_are_kept_in_order() -> None:
    guard = build_guard(
        [
            _entry("global", "credential.helper", "osxkeychain"),
            _entry("global", "credential.https://example.com.helper", "store"),
            _entry("local", "credential.helper", "!evil"),
            _entry("local", "credential.https://example.com.helper", "!evil-url"),
        ],
        {},
    )
    args = guard.config_args()
    helper_args = [a for a in args if a.startswith("credential.")]
    assert helper_args == [
        "credential.helper=",
        "credential.helper=osxkeychain",
        "credential.https://example.com.helper=store",
    ]
    assert "!evil" not in " ".join(args)
    assert "!evil-url" not in " ".join(args)


def test_submodule_state_is_kept_out_of_fetch_and_status() -> None:
    plain = GitGuard(())
    assert _submodule_argv(["fetch", "origin"], plain) == [
        "fetch", "--no-recurse-submodules", "origin",
    ]
    local = GitGuard((), local_upload_pack=True)
    assert _submodule_argv(["fetch", "--", "origin"], local) == [
        "fetch", "--no-recurse-submodules", "--upload-pack=git-upload-pack", "--", "origin",
    ]
    assert _submodule_argv(["status", "--porcelain=v2", "--branch"], plain) == [
        "status", "--ignore-submodules=dirty", "--porcelain=v2", "--branch",
    ]
    assert _submodule_argv(["switch", "dev"], local) == ["switch", "dev"]


def test_a_timeout_never_names_the_override_values(
    tmp_path: Path, monkeypatch, person_config: Path,
) -> None:
    repo = _repo(tmp_path)
    person_config.write_text('[credential "https://example.com"]\n\thelper = store\n', encoding="utf-8")
    _git(repo, "config", "credential.https://example.com.helper", "!evil")
    real_run = subprocess.run

    def _hang(command, *args, **kwargs):
        if command[1:2] != ["-c"] or "config" not in command:  # let the config read work
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("infra.git_local_config_guard.subprocess.run", _hang)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        guarded_run(repo, ["status", "--porcelain"], timeout=7)
    assert raised.value.cmd == ["git", "status", "--porcelain"]
    assert raised.value.__cause__ is None
    text = str(raised.value)
    assert "-c" not in text and "store" not in text and "hooksPath" not in text


def _submodule_top(tmp_path: Path, person_config: Path) -> tuple[Path, Path]:
    """A superproject whose submodule has its own config; returns (top, submodule)."""
    person_config.write_text('[protocol "file"]\n\tallow = always\n', encoding="utf-8")
    sub_source = _repo(tmp_path, "sub-source")
    (sub_source / ".gitattributes").write_text("*.dat filter=evil\n", encoding="utf-8")
    (sub_source / "a.dat").write_text("one\n", encoding="utf-8")
    _git(sub_source, "add", "-A")
    _git(sub_source, "commit", "-m", "sub first")
    top = _repo(tmp_path, "top")
    _git(top, "submodule", "add", str(sub_source), "sm")
    _git(top, "commit", "-m", "add submodule")
    return top, top / "sm"


def test_status_does_not_run_a_submodules_own_clean_filter(
    tmp_path: Path, person_config: Path,
) -> None:
    top, sub = _submodule_top(tmp_path, person_config)
    marker, command = _touch(tmp_path, "sub-clean")
    _git(sub, "config", "filter.evil.clean", f"{command}; cat")
    (sub / "a.dat").write_text("two\n", encoding="utf-8")  # same size, newer mtime
    os.utime(sub / "a.dat", (time.time() + 10, time.time() + 10))
    _git(top, "status", "--porcelain")
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    done = guarded_run(top, ["status", "--porcelain=v1", "--untracked-files=normal"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)


def test_switch_does_not_recurse_into_a_submodule_with_its_own_filter(
    tmp_path: Path, person_config: Path,
) -> None:
    top, sub = _submodule_top(tmp_path, person_config)
    marker, command = _touch(tmp_path, "sub-smudge")
    _git(sub, "config", "filter.evil.smudge", f"{command}; cat")
    # A second superproject branch pointing the submodule at a newer commit.
    (sub / "a.dat").write_text("two\n", encoding="utf-8")
    _git(sub, "add", "-A")
    _git(sub, *_IDENT, "commit", "-m", "sub newer")  # a clone has no identity of its own
    _git(top, "add", "sm")
    _git(top, "commit", "-m", "submodule moves")
    _git(top, "branch", "old", "HEAD~1")
    _git(top, "config", "submodule.recurse", "true")
    _git(top, "switch", "old", check=False)
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    _git(top, "switch", "dev", check=False)
    assert _ran(marker)
    done = guarded_run(top, ["switch", "old"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)
    assert _git(top, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "old"


def _signed_commit(repo: Path, message: str) -> str:
    """Write a commit object carrying a gpgsig header; return its object id."""
    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    parent = _git(repo, "rev-parse", "HEAD").stdout.strip()
    stamp = "1700000000 +0000"
    text = (
        f"tree {tree}\nparent {parent}\n"
        f"author T <t@corsia.local> {stamp}\ncommitter T <t@corsia.local> {stamp}\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n wsBBCAYACgUQFakeFakeFake\n"
        " =AAAA\n-----END PGP SIGNATURE-----\n\n" + message + "\n"
    )
    done = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=str(repo), input=text, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def test_merge_does_not_run_a_local_gpg_program(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, script = _script(tmp_path, "gpg")
    _git(repo, "config", "gpg.program", str(script))
    _git(repo, "config", "merge.verifySignatures", "true")
    oid = _signed_commit(repo, "signed")
    _git(repo, "merge", "--ff-only", "--no-edit", oid, check=False)
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    done = guarded_run(repo, ["merge", "--ff-only", "--no-edit", oid], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)


def test_merge_still_verifies_when_the_person_asked_for_it(
    tmp_path: Path, person_config: Path,
) -> None:
    repo = _repo(tmp_path)
    marker, script = _script(tmp_path, "gpg-person")
    person_config.write_text("[merge]\n\tverifySignatures = true\n", encoding="utf-8")
    _git(repo, "config", "gpg.program", str(script))
    oid = _signed_commit(repo, "signed too")
    done = guarded_run(repo, ["merge", "--ff-only", "--no-edit", oid], timeout=60)
    assert not _ran(marker)  # the repository's own program is never the verifier
    assert done.returncode != 0  # the person's verification request still applies


def test_a_helper_list_without_local_entries_is_left_alone() -> None:


    guard = build_guard([_entry("global", "credential.helper", "osxkeychain")], {})
    assert not [a for a in guard.config_args() if a.startswith("credential.")]


def test_an_empty_config_keeps_the_fixed_overrides() -> None:
    assert build_guard([], {}).config_args() == [
        "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "protocol.ext.allow=never", "-c", "submodule.recurse=false",
    ]


def test_a_config_git_cannot_read_stops_the_call_with_gits_own_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "unreadable")
    _git(repo, "config", "core.fsmonitor", command)
    (repo / ".git" / "config").write_text("not a config\n", encoding="utf-8")
    done = guarded_run(repo, ["status", "--porcelain"], timeout=60)
    assert done.returncode != 0 and "config" in done.stderr
    assert done.args == ["git", "status", "--porcelain"]
    assert not _ran(marker)


def test_a_config_read_that_hangs_stops_the_call(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    calls: list[list[str]] = []

    def _hang(command, *args, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("infra.git_local_config_guard.subprocess.run", _hang)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        guarded_run(repo, ["status", "--porcelain"], timeout=7)
    assert raised.value.cmd == ["git", "status", "--porcelain"]
    assert len(calls) == 1, "the real command must not run after the read gave up"


def test_a_local_config_based_hook_is_not_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker, command = _touch(tmp_path, "hook")
    _git(repo, "config", "hook.planted.command", command)
    _git(repo, "config", "hook.planted.event", "post-checkout")
    _git(repo, "worktree", "add", "-b", "plain", str(tmp_path / "plain"))
    if not _ran(marker):
        pytest.skip("this Git has no config-based hooks")
    done = guarded_run(repo, ["worktree", "add", "-b", "guarded", str(tmp_path / "guarded")], timeout=60)
    assert done.returncode == 0, done.stderr
    assert not _ran(marker)


def _lfs_repo(tmp_path: Path) -> Path:
    if shutil.which("git-lfs") is None:
        pytest.skip("git-lfs is not installed")
    repo = _repo(tmp_path)
    _git(repo, "config", "lfs.url", "https://lfs.invalid/x")
    _git(repo, "lfs", "install", "--local")
    _git(repo, "lfs", "track", "*.bin")
    (repo / "big.bin").write_text("payload\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "lfs")
    return repo


def test_a_local_lfs_transfer_agent_is_not_run_by_a_worktree_add(tmp_path: Path) -> None:
    repo = _lfs_repo(tmp_path)
    marker, script = _script(tmp_path, "transfer")
    _git(repo, "config", "lfs.customtransfer.planted.path", str(script))
    _git(repo, "config", "lfs.standalonetransferagent", "planted")
    shutil.rmtree(repo / ".git" / "lfs" / "objects")
    _git(repo, "worktree", "add", "--detach", str(tmp_path / "plain"), "HEAD", check=False)
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    guarded_run(repo, ["worktree", "add", "--detach", str(tmp_path / "guarded"), "HEAD"], timeout=60)
    assert not _ran(marker)


def test_a_local_lfs_extension_is_not_run_and_the_content_still_passes(tmp_path: Path) -> None:
    repo = _lfs_repo(tmp_path)
    marker = tmp_path / "marker-extension"
    script = tmp_path / "extension.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    _git(repo, "config", "lfs.extension.planted.clean", str(script))
    _git(repo, "config", "lfs.extension.planted.smudge", str(script))
    _git(repo, "config", "lfs.extension.planted.priority", "0")
    (repo / "big.bin").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "--renormalize", ".")
    assert _ran(marker), "the plant must work against plain Git or the test proves nothing"
    (repo / "big.bin").write_text("changed again\n", encoding="utf-8")
    done = guarded_run(repo, ["status", "--porcelain"], timeout=60)
    assert done.returncode == 0, done.stderr
    assert "big.bin" in done.stdout
    assert not _ran(marker)


def test_neutral_values_and_the_persons_effective_values() -> None:
    guard = build_guard(
        [
            _entry("local", "core.sshcommand", "evil"),
            _entry("local", "core.askpass", "evil"),
            _entry("local", "filter.x.clean", "evil"),
            _entry("local", "filter.lfs.smudge", "git-lfs smudge -- %f"),
            _entry("global", "filter.y.clean", "mine"),
            _entry("local", "filter.y.clean", "evil"),
        ],
        {"GIT_SSH": "/opt/my ssh"},
    )
    args = guard.config_args()
    assert "core.sshcommand='/opt/my ssh'" in args
    assert "core.askpass=" in args
    assert "filter.x.clean=" in args
    assert "filter.x.required=false" in args
    assert "filter.y.clean=mine" in args
    assert not any(a.startswith("filter.lfs.") for a in args)


def test_an_environment_that_already_carries_config_entries_is_extended(monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "me")
    env = GitGuard((("filter.a=b.clean", ""),)).environment()
    assert env is not None
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("filter.a=b.clean", "")
    assert env["GIT_CONFIG_KEY_0"] == "user.name"
    assert os.environ["GIT_CONFIG_COUNT"] == "1"
