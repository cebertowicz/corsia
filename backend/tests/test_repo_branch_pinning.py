from __future__ import annotations

import hashlib
import json

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from core.turn_core import TurnIdentity
from tests.project_ref_helpers import project_ref, select_workspace


_RECOVERY_FETCH = "fetches the configured remote"
_RECOVERY_WORKTREE = "`worktree_create`"
_RECOVERY_REMOTE_NOTE = "configured remote"
_SHELL_TURN = TurnIdentity("repo-shell-turn", 1)
_SHELL_CONTEXT = {
    "_thread_id": "repo-shell-thread",
    "_host_access_turn_identity": _SHELL_TURN,
}


def _git_repo(path: Path, branch: str = "main") -> Path:
    subprocess.run(
        [
            "git", "-c", "init.defaultRefFormat=files", "init", "-b", branch,
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _git_repo_with_commit(path: Path) -> Path:
    repo = _git_repo(path)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=CORSIA Test",
            "-c", "user.email=test@corsia.local", "commit", "-m", "initial",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _add_remote(repo: Path, name: str, url: str = "https://example.invalid/repo.git") -> None:
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", name, url],
        check=True,
        capture_output=True,
        text=True,
    )


def _linked_worktree(repo: Path, name: str = "feature") -> Path:
    worktree = repo / ".worktrees" / name
    gitdir = repo / ".git" / "worktrees" / name
    worktree.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    marker = worktree / ".git"
    marker.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{marker}\n", encoding="utf-8")
    return worktree


class _ShellProcess:
    pid = 999_995

    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _stub_shell_process(
    monkeypatch,
    shell,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> None:
    from host_access import processes

    async def spawn(*_args, **_kwargs):
        return _ShellProcess(stdout, stderr, returncode)

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        processes,
        "_process_creation_fingerprint",
        lambda _pid: "test-fingerprint",
    )
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        shell,
        "execution_envelope_profile",
        lambda *_a, **_k: "",
    )
    monkeypatch.setattr(
        shell,
        "safehouse_argv",
        lambda command, *_a, **_k: ["safehouse", "--", "bash", "-c", command],
    )


def _policy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT
        );
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )
    return db


def test_registered_repo_rows_quarantine_retargeted_or_missing_bindings(
    tmp_path,
    monkeypatch,
):
    from infra.project_repo_policy import (
        list_registered_repo_policies,
        register_repo_policy,
    )

    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project = workspace / "projects" / "demo"
    source = project / "source"
    source.mkdir(parents=True)
    repo = _git_repo(tmp_path / "app")
    retarget = _git_repo(tmp_path / "retarget")
    link = source / "app"
    link.symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'Demo', ?)",
        (project_ref("demo"),),
    )

    assert list_registered_repo_policies(workspace=workspace, db=db) == []

    register_repo_policy(
        "p1",
        "app",
        repo,
        pinned_branch="main",
        protect_base_checkout=True,
        db=db,
    )
    policy = list_registered_repo_policies(workspace=workspace, db=db)[0]
    assert (
        policy.repo_name,
        policy.path,
        policy.pinned_branch,
        policy.protect_base_checkout,
        policy.branch_pinned,
    ) == ("app", repo.resolve(), "main", True, True)

    link.unlink()
    link.symlink_to(retarget, target_is_directory=True)
    stale = list_registered_repo_policies(workspace=workspace, db=db)
    assert len(stale) == 1
    assert stale[0].path == repo.resolve()
    assert stale[0].source_binding_valid is False
    assert "source binding" in (stale[0].enforcement_error or "")

    link.unlink()
    missing = list_registered_repo_policies(workspace=workspace, db=db)
    assert len(missing) == 1
    assert missing[0].path == repo.resolve()
    assert missing[0].source_binding_valid is False
    assert "source binding" in (missing[0].enforcement_error or "")


def test_persisted_repo_path_identity_does_not_follow_replaced_symlink_chain(
    tmp_path,
    monkeypatch,
):
    from infra.project_repo_policy import (
        get_project_repo_policies,
        list_registered_repo_policies,
        register_repo_policy,
    )

    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project = workspace / "projects" / "demo"
    source = project / "source"
    source.mkdir(parents=True)
    registered = _git_repo(tmp_path / "registered")
    alias = tmp_path / "alias"
    alias.symlink_to(registered, target_is_directory=True)
    (source / "app").symlink_to(alias, target_is_directory=True)
    db = _policy_db()
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'Demo', ?)",
        (project_ref("demo"),),
    )
    register_repo_policy(
        "p1",
        "app",
        source / "app",
        pinned_branch="main",
        protect_base_checkout=True,
        db=db,
    )
    assert get_project_repo_policies("p1", db=db)["app"].path == registered
    assert len(list_registered_repo_policies(workspace=workspace, db=db)) == 1

    original = tmp_path / "registered-original"
    registered.rename(original)
    replacement = _git_repo(tmp_path / "replacement")
    registered.symlink_to(replacement, target_is_directory=True)

    assert get_project_repo_policies("p1", db=db)["app"].path == registered
    quarantined = list_registered_repo_policies(workspace=workspace, db=db)
    assert len(quarantined) == 1
    assert quarantined[0].path == registered
    assert quarantined[0].source_binding_valid is False
    assert "source binding" in (quarantined[0].enforcement_error or "")


def test_malformed_persisted_repo_paths_fail_closed(tmp_path):
    from infra.project_repo_policy import (
        get_project_repo_policies,
        list_registered_repo_policies,
    )

    db = _policy_db()
    db.execute("INSERT INTO projects (id, name) VALUES ('p1', 'Demo')")
    db.executemany(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        " protect_base_checkout) VALUES ('p1', ?, ?, NULL, 0)",
        [
            ("relative", "../outside"),
            ("noncanonical", str(tmp_path / "folder" / ".." / "outside")),
        ],
    )

    assert get_project_repo_policies("p1", db=db) == {}
    assert list_registered_repo_policies(workspace=tmp_path, db=db) == []


def test_project_repo_policy_snapshot_is_scoped_sorted_and_canonical():
    from infra import project_repo_policy

    snapshot_loader = getattr(
        project_repo_policy,
        "load_project_repo_policy_snapshot",
        None,
    )
    assert callable(snapshot_loader), "canonical snapshot loader is missing"
    db = _policy_db()
    db.executemany(
        "INSERT INTO projects (id,name) VALUES (?,?)",
        [("project-a", "Project A"), ("project-b", "Project B")],
    )
    db.executemany(
        "INSERT INTO project_repo_policies "
        "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES (?,?,?,?,?)",
        [
            ("project-a", "zeta", "/srv/zeta", None, 0),
            ("project-b", "other", "/srv/other", "main", 1),
            ("project-a", "alpha", "/srv/alpha", "dev", 1),
        ],
    )

    rows, encoded, digest = snapshot_loader("project-a", db=db)

    expected = [
        {
            "pinned_branch": "dev",
            "protect_base_checkout": True,
            "repo_name": "alpha",
            "repo_path": "/srv/alpha",
        },
        {
            "pinned_branch": None,
            "protect_base_checkout": False,
            "repo_name": "zeta",
            "repo_path": "/srv/zeta",
        },
    ]
    assert rows == expected
    assert encoded == json.dumps(
        expected,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert (
        digest
        == hashlib.sha256(
            json.dumps(
                {
                    "requesting_project_id": "project-a",
                    "repo_policy_snapshot": expected,
                    "version": 1,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("repo_name", "pinned_branch"),
    [(" repo", "dev"), ("repo", " dev ")],
)
def test_project_repo_policy_snapshot_rejects_noncanonical_whitespace(
    repo_name,
    pinned_branch,
):
    from infra import project_repo_policy

    snapshot_loader = getattr(
        project_repo_policy,
        "load_project_repo_policy_snapshot",
        None,
    )
    assert callable(snapshot_loader), "canonical snapshot loader is missing"
    db = _policy_db()
    db.execute("INSERT INTO projects (id,name) VALUES ('p1','Demo')")
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id,repo_name,repo_path,pinned_branch,protect_base_checkout) "
        "VALUES ('p1',?,?,?,1)",
        (repo_name, "/srv/repo", pinned_branch),
    )

    with pytest.raises(
        project_repo_policy.RepoPolicyQueryError,
        match="malformed",
    ):
        snapshot_loader("p1", db=db)


def test_registered_non_git_directory_is_a_full_unprotected_row(tmp_path):
    from infra.project_repo_policy import (
        list_registered_repo_policies,
        register_repo_policy,
    )

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "demo"
    source = project / "source"
    source.mkdir(parents=True)
    notes = tmp_path / "notes"
    notes.mkdir()
    (source / "notes").symlink_to(notes, target_is_directory=True)
    db = _policy_db()
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'Demo', ?)",
        (project_ref("demo"),),
    )

    register_repo_policy(
        "p1",
        "notes",
        notes,
        pinned_branch=None,
        protect_base_checkout=False,
        db=db,
    )

    policy = list_registered_repo_policies(workspace=workspace, db=db)[0]
    assert policy.path == notes.resolve()
    assert policy.pinned_branch is None
    assert policy.protect_base_checkout is False


def test_duplicate_repository_paths_coalesce_to_strict_effective_policy(
    tmp_path,
):
    from infra.project_repo_policy import coalesce_repo_policies, RepoPolicy

    repo = _git_repo(tmp_path / "repo")
    effective = coalesce_repo_policies(
        [
            RepoPolicy("p1", "app", repo.resolve(), "main", False),
            RepoPolicy("p2", "app", repo.resolve(), None, True),
        ]
    )

    assert effective == [
        RepoPolicy("p1", "app", repo.resolve(), "main", True)
    ]

    conflict = coalesce_repo_policies(
        [
            RepoPolicy("p1", "app", repo.resolve(), "main", False),
            RepoPolicy("p2", "app", repo.resolve(), "release", True),
        ]
    )
    assert len(conflict) == 1
    assert conflict[0].protect_base_checkout is True
    assert conflict[0].enforcement_error is not None
    assert "conflicting pinned branches" in conflict[0].enforcement_error


def test_repository_policy_query_failure_is_not_treated_as_no_policy(tmp_path):
    from infra.project_repo_policy import (
        list_registered_repo_policies,
        RepoPolicyQueryError,
    )

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE projects "
        "(id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT)"
    )

    with pytest.raises(RepoPolicyQueryError, match="repository policy query failed"):
        list_registered_repo_policies(workspace=tmp_path, db=db)


@pytest.mark.asyncio
async def test_shell_surfaces_repository_policy_query_failure(
    tmp_path,
    monkeypatch,
):
    from core.turn_core import TurnIdentity
    from infra.project_repo_policy import RepoPolicyQueryError
    from tools import host_access_gate, shell

    async def no_permit(**_kwargs):
        return None

    def _fail():
        raise RepoPolicyQueryError("repository policy query failed: unavailable")

    monkeypatch.setattr(shell, "list_registered_repo_policies", _fail)
    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        "printf ok",
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == (
        "Error: repository policy enforcement is unavailable: "
        "repository policy query failed: unavailable"
    )


def test_repo_policy_helpers_do_not_commit_caller_owned_transaction(tmp_path):
    from infra.project_repo_policy import clear_repo_policy, register_repo_policy

    db = _policy_db()
    db.execute("INSERT INTO projects (id, name) VALUES ('p1', 'Demo')")
    db.commit()
    repo = _git_repo(tmp_path / "repo")

    db.execute("BEGIN")
    register_repo_policy(
        "p1",
        "app",
        repo,
        pinned_branch="main",
        protect_base_checkout=True,
        db=db,
    )
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0

    register_repo_policy(
        "p1",
        "app",
        repo,
        pinned_branch="main",
        protect_base_checkout=True,
        db=db,
    )
    db.commit()
    db.execute("BEGIN")
    clear_repo_policy("p1", "app", db=db)
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 1


def test_branch_catalog_lists_local_and_only_unambiguous_configured_remote_refs(
    tmp_path,
):
    from infra.project_repo_policy import current_repo_branch, list_repo_branches

    repo = _git_repo_with_commit(tmp_path / "repo")
    _git(repo, "branch", "local-only")
    _add_remote(repo, "origin")
    _add_remote(repo, "upstream")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/remote-only", head)
    _git(repo, "update-ref", "refs/remotes/origin/shared", head)
    _git(repo, "update-ref", "refs/remotes/upstream/shared", head)

    branches = list_repo_branches(repo)

    assert current_repo_branch(repo) == "main"
    assert [
        (branch.name, branch.source, branch.remote)
        for branch in branches
    ] == [
        ("local-only", "local", None),
        ("main", "local", None),
        ("remote-only", "remote", "origin"),
    ]


def test_controlled_branch_switch_disables_hooks_and_tracks_unique_remote(
    tmp_path,
):
    from infra.project_repo_policy import (
        current_repo_branch,
        restore_repo_branch_switch,
        switch_repo_branch,
    )

    repo = _git_repo_with_commit(tmp_path / "repo")
    _git(repo, "branch", "local-only")
    hook_sentinel = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\ntouch '{hook_sentinel}'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    local_switch = switch_repo_branch(repo, "local-only")
    assert local_switch.previous_branch == "main"
    assert local_switch.created_local_branch is False
    assert current_repo_branch(repo) == "local-only"
    assert not hook_sentinel.exists()

    _add_remote(repo, "origin")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/remote-only", head)
    remote_switch = switch_repo_branch(repo, "remote-only")

    assert remote_switch.previous_branch == "local-only"
    assert remote_switch.created_local_branch is True
    assert current_repo_branch(repo) == "remote-only"
    assert _git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    ).stdout.strip() == "origin/remote-only"
    assert not hook_sentinel.exists()

    restore_repo_branch_switch(repo, remote_switch)
    assert current_repo_branch(repo) == "local-only"
    assert subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/remote-only",
        ],
        check=False,
    ).returncode == 1


def test_failed_remote_switch_restores_original_branch_and_created_ref(
    tmp_path,
    monkeypatch,
):
    from infra import project_repo_policy

    repo = _git_repo_with_commit(tmp_path / "repo")
    _add_remote(repo, "origin")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/remote-only", head)
    real_mutation = project_repo_policy._run_branch_mutation

    def _fail_after_switch(repo_path, args, *, operation):
        real_mutation(repo_path, args, operation=operation)
        if args[:4] == ["switch", "--track", "-c", "remote-only"]:
            raise ValueError("injected switch failure")

    monkeypatch.setattr(
        project_repo_policy,
        "_run_branch_mutation",
        _fail_after_switch,
    )

    with pytest.raises(ValueError, match="injected switch failure"):
        project_repo_policy.switch_repo_branch(repo, "remote-only")

    assert project_repo_policy.current_repo_branch(repo) == "main"
    assert subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/remote-only",
        ],
        check=False,
    ).returncode == 1


@pytest.mark.parametrize("failure", ["dirty", "missing", "ambiguous"])
def test_controlled_branch_switch_failure_preserves_checkout(tmp_path, failure):
    from infra.project_repo_policy import current_repo_branch, switch_repo_branch

    repo = _git_repo_with_commit(tmp_path / "repo")
    target = "missing"
    if failure == "dirty":
        _git(repo, "branch", "target")
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        target = "target"
    elif failure == "ambiguous":
        _add_remote(repo, "origin")
        _add_remote(repo, "upstream")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "update-ref", "refs/remotes/origin/shared", head)
        _git(repo, "update-ref", "refs/remotes/upstream/shared", head)
        target = "shared"

    with pytest.raises(ValueError, match=failure):
        switch_repo_branch(repo, target)

    assert current_repo_branch(repo) == "main"


def test_controlled_fast_forward_uses_one_upstream_and_exact_commit(
    tmp_path,
    monkeypatch,
):
    from infra.project_repo_policy import RepoPolicy, fast_forward_repo

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = _git_repo_with_commit(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    producer = tmp_path / "producer"
    subprocess.run(
        ["git", "clone", str(remote), str(producer)],
        check=True,
        capture_output=True,
        text=True,
    )
    (producer / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(producer, "add", "remote.txt")
    _git(
        producer,
        "-c",
        "user.name=CORSIA Test",
        "-c",
        "user.email=test@corsia.local",
        "commit",
        "-m",
        "remote",
    )
    _git(producer, "push", "origin", "main")
    upstream_oid = _git(producer, "rev-parse", "HEAD").stdout.strip()
    original_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    os.utime(lock, (0, 0))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lsof = bin_dir / "lsof"
    lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    lsof.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    hook_sentinel = tmp_path / "post-merge-ran"
    hook = repo / ".git" / "hooks" / "post-merge"
    hook.write_text(
        f"#!/bin/sh\ntouch '{hook_sentinel}'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    calls: list[tuple[list[str], str, float]] = []
    policy_reads = 0

    def _runner(repo_path, args, operation, timeout):
        calls.append((list(args), operation, timeout))
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _policies():
        nonlocal policy_reads
        policy_reads += 1
        return [policy]

    result = fast_forward_repo(
        policy,
        runner=_runner,
        policy_loader=_policies,
        timeout=20,
    )

    assert result.previous_oid == original_oid
    assert result.current_oid == upstream_oid
    assert result.branch == "main"
    assert result.upstream == "origin/main"
    assert result.updated is True
    assert [args for args, _operation, _timeout in calls if args[0] == "fetch"] == [
        ["fetch", "--", "origin"]
    ]
    merge_args = [
        args for args, _operation, _timeout in calls if args[0] == "merge"
    ]
    assert merge_args == [["merge", "--ff-only", "--no-edit", upstream_oid]]
    assert policy_reads >= 2
    assert not lock.exists()
    assert not hook_sentinel.exists()
    assert _git(repo, "status", "--porcelain").stdout == ""


@pytest.mark.asyncio
async def test_shell_routes_literal_base_pull_to_controlled_fast_forward(
    tmp_path,
    monkeypatch,
):
    from infra.project_repo_policy import RepoFastForward, RepoPolicy
    from tools import shell

    repo = _git_repo(tmp_path / "repo")
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    called = {}

    def _fast_forward(
        selected,
        *,
        runner,
        policy_loader,
        timeout,
    ):
        called["policy"] = selected
        called["runner"] = runner
        called["policies"] = policy_loader()
        called["timeout"] = timeout
        return RepoFastForward("main", "origin/main", "a" * 40, "b" * 40, True)

    async def _ordinary_shell_must_not_run(*_args, **_kwargs):
        raise AssertionError("controlled pull reached ordinary shell")

    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(shell, "fast_forward_repo_locked", _fast_forward)
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(
        shell.asyncio,
        "create_subprocess_shell",
        _ordinary_shell_must_not_run,
    )

    output = await shell.BashTool(working_dir=str(repo)).execute(
        "git pull --ff-only",
        **_SHELL_CONTEXT,
    )

    assert called["policy"] == policy
    assert called["policies"] == [policy]
    assert called["timeout"] > 0
    assert "Fast-forwarded repository 'app'" in output


def test_controlled_fast_forward_rejects_invalid_enforcement_before_git(
    tmp_path,
):
    from infra.project_repo_policy import RepoPolicy, fast_forward_repo

    repo = _git_repo(tmp_path / "repo")
    policy = RepoPolicy(
        "p1",
        "app",
        repo.resolve(),
        "main",
        True,
        "current branch mismatch",
    )

    def _runner(*_args, **_kwargs):
        raise AssertionError("invalid enforcement reached Git")

    with pytest.raises(ValueError, match="enforcement"):
        fast_forward_repo(
            policy,
            runner=_runner,
            policy_loader=lambda: [policy],
            timeout=10,
        )


def test_branch_pin_support_rejects_shared_or_non_files_git_metadata(tmp_path):
    from infra.project_repo_policy import branch_pin_support_error

    standalone = _git_repo(tmp_path / "standalone")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(
        f"gitdir: {standalone / '.git' / 'worktrees' / 'linked'}\n",
        encoding="utf-8",
    )
    reftable = _git_repo(tmp_path / "reftable")
    (reftable / ".git" / "config").write_text(
        "[extensions]\n\trefStorage = reftable\n",
        encoding="utf-8",
    )
    (reftable / ".git" / "reftable").mkdir()

    assert branch_pin_support_error(standalone) is None
    assert "standalone" in branch_pin_support_error(linked).lower()
    assert "reftable" in branch_pin_support_error(reftable).lower()


def test_branch_pin_support_tells_a_local_commondir_from_a_shared_one(tmp_path):
    from infra.project_repo_policy import branch_pin_support_error

    local = _git_repo(tmp_path / "local")
    (local / ".git" / "commondir").write_text(".\n", encoding="utf-8")
    shared = _git_repo(tmp_path / "shared")
    common = _git_repo(tmp_path / "common")
    (shared / ".git" / "commondir").write_text(f"{common / '.git'}\n", encoding="utf-8")

    assert branch_pin_support_error(local) is None
    assert "different Git or common directory" in branch_pin_support_error(shared)


def test_unsupported_pinned_repo_quarantines_metadata_when_layout_times_out(
    tmp_path,
    monkeypatch,
):
    from infra import project_repo_policy

    repo = _git_repo(tmp_path / "repo")
    policy = project_repo_policy.RepoPolicy(
        "p1",
        "app",
        repo.resolve(),
        "main",
        True,
    )
    real_run = project_repo_policy.subprocess.run

    def _timeout_layout(command, *args, **kwargs):
        if command[:2] == ["git", "config"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_layout)

    unsupported = project_repo_policy.list_unsupported_pinned_repos([policy])

    assert len(unsupported) == 1
    assert "Git ref storage verification timed out" in unsupported[0].reason
    assert unsupported[0].metadata_roots == (repo / ".git",)


def test_branch_pin_support_does_not_require_show_ref_format(tmp_path, monkeypatch):
    from infra import project_repo_policy

    repo = _git_repo(tmp_path / "standalone")
    git_dir = repo / ".git"

    def _older_git(args, **_kwargs):
        assert "--show-ref-format" not in args
        if "config" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(
            args,
            0,
            f"{repo}\n{git_dir}\n{git_dir}\n",
            "",
        )

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _older_git)

    assert project_repo_policy.branch_pin_support_error(repo) is None


def test_repo_write_gate_blocks_base_and_allows_verified_worktree(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config

    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo)
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [policy],
    )

    error = sandbox_config.check_repo_write_path(repo / "src" / "app.py")
    assert error is not None
    assert "branch-pinned" in error
    assert _RECOVERY_FETCH in error
    assert _RECOVERY_WORKTREE in error
    assert _RECOVERY_REMOTE_NOTE in error
    assert "feature" not in error.lower()
    assert sandbox_config.check_repo_write_path(worktree / "src" / "app.py") is None


@pytest.mark.asyncio
async def test_file_tools_read_pinned_base_but_write_only_in_worktree(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config
    from tools.filesystem_rw import ReadFileTool, WriteFileTool

    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [policy],
    )
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    read = await ReadFileTool().execute(path=str(repo / "README.md"))
    blocked = await WriteFileTool().execute(
        path=str(repo / "README.md"), content="changed\n"
    )
    written = await WriteFileTool().execute(
        path=str(worktree / "new.txt"), content="worktree\n"
    )
    original_marker = (worktree / ".git").read_text(encoding="utf-8")
    marker_write = await WriteFileTool().execute(
        path=str(worktree / ".git"),
        content="gitdir: /tmp/attacker\n",
    )

    assert "base" in read
    assert "branch-pinned" in blocked
    assert (repo / "README.md").read_text(encoding="utf-8") == "base\n"
    assert written.startswith("Successfully wrote")
    assert (worktree / "new.txt").read_text(encoding="utf-8") == "worktree\n"
    assert "Git control metadata" in marker_write
    assert (worktree / ".git").read_text(encoding="utf-8") == original_marker


@pytest.mark.asyncio
async def test_shell_fails_closed_when_pinned_repo_cannot_be_sandboxed(
    tmp_path, monkeypatch
):
    from core.turn_core import TurnIdentity
    from infra.project_repo_policy import RepoPolicy
    from tools import host_access_gate, shell

    async def no_permit(**_kwargs):
        return None

    repo = _git_repo(tmp_path / "repo")
    target = repo / "base.txt"
    target.write_text("base\n", encoding="utf-8")
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    result = await shell.BashTool(working_dir=str(repo)).execute(
        "printf hacked > base.txt",
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert "backend PATH: /usr/bin:/bin" in result
    assert "install Agent Safehouse" in result
    assert "CORSIA service PATH" in result
    assert target.read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_shell_keeps_reads_available_when_pinned_git_metadata_is_unsupported(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell, shell_guard

    repo = tmp_path / "linked-worktree"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /tmp/shared.git/worktrees/linked\n")
    monkeypatch.setattr(
        shell,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    _stub_shell_process(monkeypatch, shell, stdout="read-ok")

    result = await shell.BashTool(working_dir=str(repo)).execute(
        "printf read-ok",
        **_SHELL_CONTEXT,
    )

    assert result == "read-ok"


@pytest.mark.parametrize("symlink_marker", [False, True])
@pytest.mark.asyncio
async def test_file_tools_fail_closed_when_pinned_git_metadata_is_not_protectable(
    tmp_path, monkeypatch, symlink_marker
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config
    from tools.filesystem import WriteFileTool

    owner = _git_repo_with_commit(tmp_path / "owner")
    repo = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "-C", str(owner), "worktree", "add", "-b", "feature", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    marker = repo / ".git"
    if symlink_marker:
        raw_gitdir = marker.read_text(encoding="utf-8").strip().split(":", 1)[1]
        git_dir = Path(raw_gitdir.strip()).resolve()
        marker.unlink()
        marker.symlink_to(git_dir, target_is_directory=True)
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        capture_output=True,
        text=True,
    ).returncode == 0
    shared_ref = owner / ".git" / "refs" / "heads" / "feature"
    original_ref = shared_ref.read_text(encoding="utf-8")
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    result = await WriteFileTool().execute(
        path=str(shared_ref),
        content="1" * 40 + "\n",
    )

    assert "branch-pinned" in result
    assert "standalone" in result.lower()
    assert "unpin" in result.lower()
    assert shared_ref.read_text(encoding="utf-8") == original_ref


@pytest.mark.asyncio
async def test_invalid_git_symlink_does_not_quarantine_unrelated_paths(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config, shell_guard
    from tools.filesystem import WriteFileTool

    repo = tmp_path / "invalid-repo"
    repo.mkdir()
    (repo / ".git").symlink_to(Path("/"), target_is_directory=True)
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [policy],
    )
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path.resolve()])
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    result = await WriteFileTool().execute(
        path=str(tmp_path / "unrelated.txt"),
        content="allowed\n",
    )
    profile = shell_guard.pinned_repo_sandbox_profile("printf read-ok", str(repo))

    assert result.startswith("Successfully wrote")
    assert '(deny file-write* (subpath "/"))' not in profile


@pytest.mark.asyncio
async def test_file_tools_reject_pinned_repo_with_external_branch_refs(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config
    from tools.filesystem import WriteFileTool

    repo = _git_repo(tmp_path / "repo")
    refs = repo / ".git" / "refs"
    external_refs = tmp_path / "external-refs"
    refs.rename(external_refs)
    refs.symlink_to(external_refs, target_is_directory=True)
    branch_ref = external_refs / "heads" / "main"
    branch_ref.write_text("0" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path.resolve()])
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    result = await WriteFileTool().execute(
        path=str(branch_ref),
        content="1" * 40 + "\n",
    )
    unrelated = await WriteFileTool().execute(
        path=str(tmp_path / "unrelated.txt"),
        content="allowed\n",
    )

    assert "branch-pinned" in result
    assert "metadata" in result.lower()
    assert branch_ref.read_text(encoding="utf-8") == "0" * 40 + "\n"
    assert unrelated.startswith("Successfully wrote")
    assert (tmp_path / "unrelated.txt").read_text(encoding="utf-8") == "allowed\n"


def test_unsupported_pinned_repo_profile_quarantines_external_git_metadata(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    refs = repo / ".git" / "refs"
    external_refs = tmp_path / "external-refs"
    refs.rename(external_refs)
    refs.symlink_to(external_refs, target_is_directory=True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    profile = shell_guard.pinned_repo_sandbox_profile("printf read-ok", str(repo))

    assert f'(deny file-write* (subpath "{external_refs.resolve()}"))' in profile


def test_unsupported_pull_profile_keeps_git_metadata_read_only(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    refs = repo / ".git" / "refs"
    external_refs = tmp_path / "external-refs"
    refs.rename(external_refs)
    refs.symlink_to(external_refs, target_is_directory=True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "git pull --ff-only",
        str(repo),
    )
    allow_repo = f'(allow file-read* file-write* (subpath "{repo.resolve()}"))'
    deny_git = f'(deny file-write* (subpath "{(repo / ".git").resolve()}"))'

    assert profile.rfind(deny_git) > profile.rfind(allow_repo)
    if not shutil.which("safehouse"):
        pytest.skip("safehouse is not installed")
    head = repo / ".git" / "HEAD"
    original_head = head.read_text(encoding="utf-8")
    profile_path = tmp_path / "unsupported-pull.sb"
    profile_path.write_text(profile, encoding="utf-8")
    result = subprocess.run(
        [
            "safehouse",
            f"--workdir={tmp_path.resolve()}",
            f"--append-profile={profile_path}",
            "--",
            "bash",
            "-c",
            'printf changed > "$1"',
            "_",
            str(head),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert head.read_text(encoding="utf-8") == original_head


@pytest.mark.asyncio
async def test_pinned_base_sandbox_denial_adds_canonical_worktree_guidance(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config, shell

    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [policy],
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    _stub_shell_process(
        monkeypatch,
        shell,
        stderr="Operation not permitted",
        returncode=1,
    )

    blocked = await shell.BashTool(working_dir=str(repo)).execute(
        "touch base.txt",
        **_SHELL_CONTEXT,
    )
    unrelated = await shell.BashTool(working_dir=str(outside)).execute(
        "touch outside.txt",
        **_SHELL_CONTEXT,
    )

    assert "Operation not permitted" in blocked
    assert "branch-pinned" in blocked
    assert ".worktrees" in blocked
    assert "disable base-checkout protection" in blocked.lower()
    assert "branch-pinned" not in unrelated


@pytest.mark.asyncio
async def test_success_with_nonfatal_sandbox_stderr_adds_no_denial_guidance(
    tmp_path, monkeypatch,
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config, shell

    repo = _git_repo(tmp_path / "repo")
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [policy],
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    _stub_shell_process(
        monkeypatch,
        shell,
        stderr="Operation not permitted",
        returncode=0,
    )

    result = await shell.BashTool(working_dir=str(repo)).execute(
        "git worktree prune",
        **_SHELL_CONTEXT,
    )

    assert "Operation not permitted" in result
    assert "branch-pinned" not in result
    assert "host access" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "withheld_reason", "shows_guidance"),
    [
        (1, "CORSIA AF_UNIX temp root is unavailable or not private", True),
        (0, "CORSIA AF_UNIX temp root is unavailable or not private", False),
        (1, None, False),
    ],
)
async def test_sandbox_denial_explains_withheld_bind_root_only_when_relevant(
    tmp_path, monkeypatch, returncode, withheld_reason, shows_guidance,
):
    from tools import shell
    from tools import shell_output

    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(
        shell_output,
        "thread_bind_root_withheld_reason",
        lambda _thread_id: withheld_reason,
    )
    _stub_shell_process(
        monkeypatch, shell, stderr="Operation not permitted", returncode=returncode,
    )

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        "touch socket", **_SHELL_CONTEXT,
    )

    guidance = "AF_UNIX bind root not granted for this shell"
    assert (guidance in result) is shows_guidance


def test_shell_guard_uses_registered_pin_not_corsia_specific_paths(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "customer-repo", branch="release")
    worktree = _linked_worktree(repo)
    policy = RepoPolicy("customer", "web", repo.resolve(), "release", True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    blocked = shell_guard.check_worktree_targets("git switch feature", str(repo))
    assert blocked is not None
    assert "branch-pinned" in blocked
    assert _RECOVERY_FETCH in blocked
    assert _RECOVERY_WORKTREE in blocked
    assert shell_guard.check_worktree_targets(
        "git switch renamed", str(worktree)
    ) is None

    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("customer", "web", repo.resolve(), None, False)],
    )
    assert shell_guard.check_worktree_targets("git switch feature", str(repo)) is None


@pytest.mark.parametrize(
    "command",
    [
        "git branch new-branch",
        "git branch new-branch && git switch new-branch",
    ],
)
def test_pinned_base_branch_mutations_return_direct_recovery_commands(
    command, tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo", branch="release")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("customer", "web", repo.resolve(), "release", True)],
    )

    blocked = shell_guard.check_worktree_targets(command, str(repo))

    assert blocked is not None
    assert "direct" in blocked.lower()
    assert _RECOVERY_FETCH in blocked
    assert _RECOVERY_WORKTREE in blocked
    assert "feature" not in blocked.lower()


def test_pinned_base_allows_literal_remote_fetch_and_fast_forward_pull(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo", branch="release")
    _add_remote(repo, "origin")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("customer", "web", repo.resolve(), "release", True)],
    )

    blocked_pull = shell_guard.check_worktree_targets("git pull", str(repo))
    blocked_fetches = [
        shell_guard.check_worktree_targets(command, str(repo))
        for command in (
            "git fetch --all",
            "git fetch upstream refs/heads/release",
            "git fetch ../upstream",
            "git fetch foo..bar",
            "git fetch foo.lock",
            "git fetch ..",
            "git fetch foo.",
            "git fetch FOO.LOCK",
            "git fetch https://evil/repo",
            "git fetch ./x",
            "git fetch origin dev",
            "git fetch notaremote",
        )
    ]

    assert blocked_pull is not None
    assert "git pull --ff-only" in blocked_pull
    assert all(blocked is not None for blocked in blocked_fetches)
    assert shell_guard.check_worktree_targets("git fetch", str(repo)) is None
    assert shell_guard.check_worktree_targets("git fetch origin", str(repo)) is None
    assert shell_guard.check_worktree_targets("git pull --ff-only", str(repo)) is None


def test_pinned_base_allows_fetch_for_remote_configured_via_include(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo", branch="release")
    included = tmp_path / "included.gitconfig"
    included.write_text(
        '[remote "origin"]\n'
        "\turl = https://example.invalid/repo.git\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "include.path", str(included)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("customer", "web", repo.resolve(), "release", True)],
    )

    assert shell_guard.check_worktree_targets("git fetch origin", str(repo)) is None
    assert shell_guard.check_worktree_targets(
        "git fetch notaremote", str(repo)
    ) is not None


def test_pinned_base_ignores_remotes_configured_only_globally(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo", branch="release")
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    subprocess.run(
        [
            "git", "config",
            "--file", str(fake_home / ".gitconfig"),
            "remote.origin.url", "https://global.invalid/repo.git",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / ".gitconfig"))
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("customer", "web", repo.resolve(), "release", True)],
    )

    assert shell_guard.check_worktree_targets(
        "git fetch origin", str(repo)
    ) is not None


def test_pinned_repo_sandbox_profile_denies_base_and_allows_worktree(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "workspace" / "project" / "source" / "app")
    worktree = _linked_worktree(repo)
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    profile = shell_guard.pinned_repo_sandbox_profile("pytest", str(worktree))
    assert f'(deny file-write* (subpath "{repo.resolve()}"))' in profile
    assert (
        f'(allow file-read* file-write* (subpath "{worktree.resolve()}"))'
        in profile
    )

    if not shutil.which("safehouse"):
        pytest.skip("safehouse is not installed")
    base_file = repo / "base.txt"
    worktree_file = worktree / "worktree.txt"
    base_file.write_text("base\n", encoding="utf-8")
    worktree_file.write_text("worktree\n", encoding="utf-8")
    profile_path = tmp_path / "pin.sb"
    profile_path.write_text(profile, encoding="utf-8")

    result = subprocess.run(
        [
            "safehouse",
            "--workdir=",
            f"--append-profile={profile_path}",
            "--",
            "bash",
            "-c",
            'printf changed > "$1"; printf wtchanged > "$2"',
            "_",
            str(base_file),
            str(worktree_file),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0 or "Operation not permitted" in result.stderr
    assert base_file.read_text(encoding="utf-8") == "base\n"
    assert worktree_file.read_text(encoding="utf-8") == "wtchanged"


def test_recovery_commands_receive_only_narrow_write_exceptions(tmp_path, monkeypatch):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    _add_remote(repo, "origin")
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    ordinary = shell_guard.pinned_repo_sandbox_profile("pytest", str(repo))
    unsafe_pull = shell_guard.pinned_repo_sandbox_profile("git pull", str(repo))
    pull = shell_guard.pinned_repo_sandbox_profile("git pull --ff-only", str(repo))
    fetch = shell_guard.pinned_repo_sandbox_profile("git fetch origin", str(repo))
    upstream_fetch = shell_guard.pinned_repo_sandbox_profile(
        "git fetch upstream", str(repo)
    )
    trailing_dot_fetch = shell_guard.pinned_repo_sandbox_profile(
        "git fetch foo.", str(repo)
    )
    uppercase_lock_fetch = shell_guard.pinned_repo_sandbox_profile(
        "git fetch FOO.LOCK", str(repo)
    )
    worktree_add = shell_guard.pinned_repo_sandbox_profile(
        "git worktree add --no-track -b new-branch "
        ".worktrees/resume origin/release",
        str(repo),
    )
    upstream_worktree_add = shell_guard.pinned_repo_sandbox_profile(
        "git worktree add --no-track -b new-branch "
        ".worktrees/resume upstream/release",
        str(repo),
    )
    composed = shell_guard.pinned_repo_sandbox_profile(
        "git pull --ff-only && printf bad > app.py", str(repo)
    )
    rejected_fetches = [
        shell_guard.pinned_repo_sandbox_profile(command, str(repo))
        for command in (
            "git fetch --all",
            "git fetch upstream refs/heads/release",
            "git fetch upstream && printf bad > app.py",
            "git fetch ../upstream",
            "git fetch foo..bar",
            "git fetch foo.lock",
        )
    ]

    repo_allow = f'(allow file-read* file-write* (subpath "{repo.resolve()}"))'
    git_allow = f'(allow file-read* file-write* (subpath "{repo / ".git"}"))'
    worktrees_allow = (
        f'(allow file-read* file-write* (subpath "{repo / ".worktrees"}"))'
    )
    repo_literal_allow = f'(allow file-read* file-write* (literal "{repo.resolve()}"))'
    assert repo_allow not in ordinary
    assert repo_allow not in unsafe_pull
    assert repo_allow not in pull
    assert repo_allow not in composed
    assert git_allow in fetch
    assert git_allow not in upstream_fetch
    assert git_allow not in trailing_dot_fetch
    assert git_allow not in uppercase_lock_fetch
    assert worktrees_allow not in fetch
    assert worktrees_allow not in upstream_fetch
    assert worktrees_allow not in trailing_dot_fetch
    assert worktrees_allow not in uppercase_lock_fetch
    assert repo_literal_allow not in fetch
    assert repo_literal_allow not in upstream_fetch
    assert repo_literal_allow not in trailing_dot_fetch
    assert repo_literal_allow not in uppercase_lock_fetch
    assert all(git_allow not in profile for profile in rejected_fetches)
    assert git_allow not in worktree_add
    assert worktrees_allow not in worktree_add
    assert repo_literal_allow not in worktree_add
    assert repo_allow not in worktree_add
    assert git_allow not in upstream_worktree_add
    assert worktrees_allow not in upstream_worktree_add
    assert repo_literal_allow not in upstream_worktree_add
    assert repo_allow not in upstream_worktree_add
    assert shell_guard.check_worktree_targets("git fetch origin", str(repo)) is None
    assert (
        shell_guard.check_worktree_targets("git fetch upstream", str(repo))
        is not None
    )
    origin_error = shell_guard.check_worktree_targets(
        "git worktree add --no-track -b new-branch "
        ".worktrees/resume origin/release",
        str(repo),
    )
    upstream_error = shell_guard.check_worktree_targets(
        "git worktree add --no-track -b new-branch "
        ".worktrees/resume upstream/release",
        str(repo),
    )
    assert origin_error is not None and "worktree_create" in origin_error
    assert upstream_error is not None and "worktree_create" in upstream_error


@pytest.mark.parametrize(
    "branch_options",
    [
        "-b feature",
        "-B feature",
        "-bfeature",
        "-Bfeature",
        "-fbfeature",
        "-fBfeature",
        "-fb feature",
        "-fB feature",
        "--no-track --track -b feature",
        "--no-track --track=direct -b feature",
        "--no-track --tr -b feature",
        "--guess-remote",
    ],
)
def test_new_worktree_branch_requires_no_track_before_git_runs(
    tmp_path, monkeypatch, branch_options,
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", False)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    unsafe = shell_guard.check_worktree_targets(
        f"git worktree add {branch_options} .worktrees/feature origin/main",
        str(repo),
    )
    safe = shell_guard.check_worktree_targets(
        "git worktree add --no-track -b feature "
        ".worktrees/feature origin/main",
        str(repo),
    )

    assert unsafe is not None
    assert "before Git made any changes" in unsafe
    assert "--no-track" in unsafe
    assert "host access" not in unsafe.lower()
    assert safe is None
    assert shell_guard.check_worktree_targets(
        "git worktree add --no-track -fb feature "
        ".worktrees/feature HEAD",
        str(repo),
    ) is None
    unsafe_profile = shell_guard.pinned_repo_sandbox_profile(
        f"git worktree add {branch_options} .worktrees/feature origin/main",
        str(repo),
    )
    assert (
        f'(allow file-read* file-write* (subpath "{repo / ".git"}"))'
        not in unsafe_profile
    )


def test_existing_local_branch_can_be_reattached_without_tracking(
    tmp_path, monkeypatch,
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo_with_commit(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "branch", "resume"],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    assert shell_guard.check_worktree_targets(
        "git worktree add .worktrees/resume resume",
        str(repo),
    ) is None
    ambiguous = shell_guard.check_worktree_targets(
        "git worktree add .worktrees/implicit",
        str(repo),
    )
    assert ambiguous is not None
    assert "before Git made any changes" in ambiguous


def test_git_write_exceptions_require_literal_trusted_git(tmp_path, monkeypatch):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    fake_git = shell_guard.pinned_repo_sandbox_profile(
        "./git pull --ff-only", str(repo)
    )
    substituted = shell_guard.pinned_repo_sandbox_profile(
        f"git fetch `touch {repo / '.git' / 'PWNED'}`", str(repo)
    )
    upload_pack = shell_guard.pinned_repo_sandbox_profile(
        f"git fetch --upload-pack='touch {repo / '.git' / 'PWNED'}'", str(repo)
    )

    assert f'(allow file-read* file-write* (subpath "{repo.resolve()}"))' not in fake_git
    assert f'(allow file-read* file-write* (subpath "{repo / ".git"}"))' not in substituted
    assert f'(allow file-read* file-write* (subpath "{repo / ".git"}"))' not in upload_pack
    assert shell_guard.check_worktree_targets(
        "git fetch --upload-pack=touch", str(repo)
    ) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git worktree lock .worktrees/feature",
        "git worktree move .worktrees/feature .worktrees/renamed",
        "git worktree prune",
        "git worktree remove .worktrees/feature",
        "git worktree repair .worktrees/feature",
        "git worktree unlock .worktrees/feature",
    ],
)
def test_worktree_management_receives_narrow_write_exceptions(
    tmp_path, monkeypatch, command
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(command, str(repo))

    assert f'(allow file-read* file-write* (subpath "{repo / ".git"}"))' in profile
    assert f'(allow file-read* file-write* (subpath "{repo / ".worktrees"}"))' in profile
    for protected in (
        repo / ".git" / "config",
        repo / ".git" / "config.lock",
        repo / ".git" / "packed-refs",
    ):
        assert f'(deny file-write* (literal "{protected}"))' in profile
    packed_refs_lock_deny = (
        f'(deny file-write* (literal "{repo / ".git" / "packed-refs.lock"}"))'
    )
    assert packed_refs_lock_deny in profile
    assert f'(deny file-write* (subpath "{repo / ".git" / "hooks"}"))' in profile


@pytest.mark.parametrize("action", ["lock", "remove", "repair", "unlock"])
def test_worktree_lifecycle_paths_cannot_escape_registered_root(
    tmp_path, monkeypatch, action
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    blocked = shell_guard.check_worktree_targets(
        f"git worktree {action} {outside}", str(repo)
    )

    assert blocked is not None
    assert ".worktrees" in blocked


def test_worktree_move_destination_cannot_escape_registered_root(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo)
    outside = tmp_path / "moved-feature"
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    blocked = shell_guard.check_worktree_targets(
        f"git worktree move {worktree} {outside}", str(repo)
    )

    assert blocked is not None
    assert ".worktrees" in blocked


def test_worktree_paths_reject_shell_parameter_expansion(tmp_path, monkeypatch):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    blocked = shell_guard.check_worktree_targets(
        "git worktree move .worktrees/feature "
        ".worktrees/${ONE:-..}/${TWO:-..}/moved",
        str(repo),
    )

    assert blocked is not None
    assert "direct git worktree" in blocked


def test_privileged_git_rejects_shell_redirection(tmp_path, monkeypatch):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    command = "git worktree prune > .git/config"

    blocked = shell_guard.check_worktree_targets(command, str(repo))
    profile = shell_guard.pinned_repo_sandbox_profile(command, str(repo))

    assert blocked is not None
    assert "direct git worktree" in blocked
    assert f'(allow file-read* file-write* (subpath "{repo / ".git"}"))' not in profile


@pytest.mark.asyncio
@pytest.mark.integration
async def test_shell_runs_real_worktree_lifecycle_without_switching_base(
    tmp_path, monkeypatch
):
    if not shutil.which("safehouse"):
        pytest.skip("safehouse is not installed")

    from infra.project_repo_policy import RepoPolicy
    from tools import shell, shell_guard, worktrees

    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "dev", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "base"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    policy = RepoPolicy("p1", "app", repo.resolve(), "dev", True)
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_rw", lambda: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(
        worktrees, "list_registered_repo_policies", lambda **_kwargs: [policy]
    )
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)

    created = await worktrees.WorktreeCreateTool().execute(
        repo_name="app",
        branch="feature",
        descriptor="feature",
        base_branch="dev",
        _thread_id="repo-shell-thread",
        _thread_project_id="p1",
    )
    assert not created.startswith("Error:"), created

    tool = shell.BashTool(working_dir=str(repo))
    commands = [
        "git worktree lock .worktrees/repo-shell-thread-feature",
        "git worktree unlock .worktrees/repo-shell-thread-feature",
        "git worktree move .worktrees/repo-shell-thread-feature .worktrees/renamed",
        "git worktree remove .worktrees/renamed",
        "git worktree add .worktrees/resumed feature",
        "git worktree remove .worktrees/resumed",
    ]
    outputs = [
        await tool.execute(command, **_SHELL_CONTEXT)
        for command in commands
    ]

    assert all("Exit code:" not in output for output in outputs), outputs
    assert all("Operation not permitted" not in output for output in outputs), outputs
    assert all("branch-pinned" not in output for output in outputs), outputs
    assert subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "dev"
    assert not (repo / ".worktrees" / "renamed").exists()
    upstream = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "branch.feature.remote"],
        capture_output=True,
        text=True,
    )
    assert upstream.returncode == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_shell_runs_real_fast_forward_pull_on_pinned_base(
    tmp_path, monkeypatch
):
    if not shutil.which("safehouse"):
        pytest.skip("safehouse is not installed")

    from infra.project_repo_policy import RepoPolicy
    from tools import shell, shell_guard

    upstream = _git_repo_with_commit(tmp_path / "upstream")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(remote), str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    (upstream / "tracked.txt").write_text("updated\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(upstream), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git", "-C", str(upstream), "-c", "user.name=CORSIA Test",
            "-c", "user.email=test@corsia.local", "commit", "-m", "update",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(upstream), "push", str(remote), "main"],
        check=True,
        capture_output=True,
        text=True,
    )

    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [policy])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_rw", lambda: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [policy])
    from tests.shell_platform_helpers import use_mac_shell
    use_mac_shell(monkeypatch)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)

    output = await shell.BashTool(working_dir=str(repo)).execute(
        "git pull --ff-only",
        **_SHELL_CONTEXT,
    )

    assert "Exit code:" not in output, output
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "updated\n"


def test_worktree_git_access_does_not_reopen_base_git_identity(
    tmp_path, monkeypatch
):
    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo(tmp_path / "repo", branch="dev")
    worktree = _linked_worktree(repo)
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    worktree_commit = shell_guard.pinned_repo_sandbox_profile(
        "git commit -m change", str(worktree)
    )
    base_pull = shell_guard.pinned_repo_sandbox_profile(
        "git pull --ff-only", str(repo)
    )
    redirected_base_commit = shell_guard.pinned_repo_sandbox_profile(
        f"git -C {repo} commit -m bad", str(worktree)
    )
    git_allow = f'(allow file-read* file-write* (subpath "{repo / ".git"}"))'

    assert git_allow in worktree_commit
    assert f'(deny file-write* (literal "{repo / ".git" / "HEAD"}"))' in worktree_commit
    assert (
        f'(deny file-write* (literal "{repo / ".git" / "refs/heads/dev"}"))'
        in worktree_commit
    )
    for protected in (
        repo / ".git" / "config",
        repo / ".git" / "config.lock",
        repo / ".git" / "packed-refs",
        repo / ".git" / "packed-refs.lock",
    ):
        deny = f'(deny file-write* (literal "{protected}"))'
        assert worktree_commit.index(deny) > worktree_commit.index(git_allow)
        assert deny in base_pull
    hooks_deny = f'(deny file-write* (subpath "{repo / ".git" / "hooks"}"))'
    marker_deny = f'(deny file-write* (literal "{worktree / ".git"}"))'
    assert worktree_commit.index(hooks_deny) > worktree_commit.index(git_allow)
    assert worktree_commit.index(marker_deny) > worktree_commit.index(git_allow)
    assert hooks_deny in base_pull
    assert (
        f'(allow file-read* file-write* (subpath "{repo.resolve()}"))'
        not in base_pull
    )
    assert git_allow not in redirected_base_commit


def test_real_worktree_git_profile_protects_common_control_files(
    tmp_path, monkeypatch
):
    if not shutil.which("safehouse"):
        pytest.skip("safehouse is not installed")

    from infra.project_repo_policy import RepoPolicy
    from tools import shell_guard

    repo = _git_repo_with_commit(tmp_path / "repo")
    worktree = repo / ".worktrees" / "feature"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "git commit -m change", str(worktree)
    )
    profile_path = tmp_path / "worktree-git.sb"
    profile_path.write_text(profile, encoding="utf-8")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    protected = [
        repo / ".git" / "config",
        repo / ".git" / "packed-refs",
        hooks / "post-merge",
    ]
    original = {
        path: path.read_bytes() if path.exists() else None
        for path in protected
    }
    write_attempt = subprocess.run(
        [
            "safehouse",
            "--workdir=",
            f"--append-profile={profile_path}",
            "--",
            "bash",
            "-c",
            'for path do printf pwned > "$path" || exit; done',
            "_",
            *(str(path) for path in protected),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert write_attempt.returncode != 0
    for path, content in original.items():
        assert (path.read_bytes() if path.exists() else None) == content

    changed = worktree / "changed.txt"
    changed.write_text("changed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", changed.name],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = subprocess.run(
        [
            "safehouse",
            "--workdir=",
            f"--append-profile={profile_path}",
            "--",
            "git",
            "commit",
            "-m",
            "change",
        ],
        cwd=worktree,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert commit.returncode == 0, commit.stderr
