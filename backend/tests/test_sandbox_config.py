import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.project_ref_helpers import project_ref, select_workspace

from infra.project_repo_policy import RepoPolicy


def _registered_external_repo(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "Documents"
    source = workspace / "projects" / "demo" / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "code" / "demo"
    (repo / ".git").mkdir(parents=True)
    (source / "demo").symlink_to(repo, target_is_directory=True)
    return workspace, repo.resolve()


def _policy_db(
    project_path: Path,
    *,
    canonical: bool,
) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE projects "
        "(id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT)"
    )
    if canonical:
        db.execute(
            """
            CREATE TABLE project_repo_policies (
                project_id TEXT NOT NULL,
                repo_name TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                pinned_branch TEXT,
                protect_base_checkout INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (project_id, repo_name)
            )
            """
        )
    else:
        db.execute(
            """
            CREATE TABLE project_repo_policies (
                project_id TEXT NOT NULL,
                repo_name TEXT NOT NULL,
                branch_pinned INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (project_id, repo_name)
            )
            """
        )
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'Demo', ?)",
        (project_ref(project_path.name),),
    )
    return db


@pytest.fixture
def short_system_temp():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
def _fresh_withheld_bind_roots(monkeypatch):
    from tools import unix_socket_policy

    monkeypatch.setattr(unix_socket_policy, "_BIND_ROOT_WITHHELD_REASONS", {})


def _configure_sandbox(
    monkeypatch,
    sandbox_config,
    workspace: Path,
    policies,
) -> None:
    provider = policies if callable(policies) else lambda: policies
    monkeypatch.setattr(sandbox_config, "WORKSPACE", workspace)
    select_workspace(monkeypatch, workspace)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        provider,
    )


def test_unix_socket_grants_are_scoped_to_the_authenticated_thread(
    monkeypatch, tmp_path, short_system_temp,
):
    from tools import sandbox_config, unix_socket_policy

    workspace = tmp_path / "workspace"
    temp_parent = short_system_temp
    for directory in (workspace, workspace / ".corsia" / "context", workspace / ".corsia" / "skills"):
        directory.mkdir(parents=True, exist_ok=True)
    _configure_sandbox(monkeypatch, sandbox_config, workspace, [])
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(temp_parent))

    first_root = unix_socket_policy.corsia_temp_root_for_bind("thread-one")
    second_root = unix_socket_policy.corsia_temp_root_for_bind("thread-two")
    temp_root = unix_socket_policy.corsia_temp_root()

    first_grants = sandbox_config.get_sandbox_add_dirs_rw("thread-one")
    second_grants = sandbox_config.get_sandbox_add_dirs_rw("thread-two")
    assert first_root in first_grants
    assert second_root not in first_grants
    assert second_root in second_grants
    assert first_root not in second_grants
    assert temp_root not in first_grants
    assert temp_parent not in first_grants
    assert temp_root.stat().st_mode & 0o777 == 0o700
    assert first_root.stat().st_mode & 0o777 == 0o700
    assert unix_socket_policy.get_unix_socket_allowed_roots("thread-one") == [
        workspace.resolve(), first_root,
    ]


def test_backend_browser_socket_dir_is_outside_connectable_roots(
    monkeypatch, tmp_path, short_system_temp,
):
    """Granting connect under these roots must not reach the backend's browser daemon."""
    from host_access import browser_identity
    from tools import unix_socket_policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(short_system_temp))

    socket_dir = browser_identity.socket_dir_for_namespace("namespace", None).resolve()
    for root in unix_socket_policy.get_unix_socket_allowed_roots("thread-one"):
        assert not socket_dir.is_relative_to(root)
        assert not root.is_relative_to(socket_dir)


def test_corsia_temp_root_refuses_symlink_or_foreign_owner(monkeypatch, tmp_path):
    from tools import unix_socket_policy

    temp_parent = tmp_path / "system-temp"
    target = tmp_path / "target"
    temp_parent.mkdir()
    target.mkdir()
    root = temp_parent / unix_socket_policy._TEMP_ROOT_NAME
    root.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(temp_parent))

    with pytest.raises(RuntimeError, match="unavailable or not private"):
        unix_socket_policy.corsia_temp_root()

    root.unlink()
    root.mkdir(mode=0o700)
    foreign_uid = root.stat().st_uid + 1
    monkeypatch.setattr(unix_socket_policy.os, "getuid", lambda: foreign_uid)
    with pytest.raises(RuntimeError, match="unavailable or not private"):
        unix_socket_policy.corsia_temp_root()


@pytest.mark.parametrize("thread_id", ["..", ".", "a/b", "", "a" * 65])
def test_corsia_temp_root_for_bind_rejects_invalid_thread_identity(
    monkeypatch, tmp_path, thread_id,
):
    from tools import unix_socket_policy

    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match="requires a valid thread identity"):
        unix_socket_policy.corsia_temp_root_for_bind(thread_id)
    # An identity breach stops the shell rather than withholding one grant.
    with pytest.raises(RuntimeError, match="requires a valid thread identity"):
        unix_socket_policy.thread_bind_roots(thread_id)
    assert unix_socket_policy.thread_bind_root_withheld_reason(thread_id) is None


def test_corsia_temp_root_for_bind_accepts_thread_identity(monkeypatch, short_system_temp):
    from tools import unix_socket_policy

    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(short_system_temp))
    assert unix_socket_policy.corsia_temp_root_for_bind("thread_1-abc").is_dir()


def test_corsia_temp_root_for_bind_fits_uuid_thread_under_macos_user_temp(
    monkeypatch, short_system_temp,
):
    from tools import unix_socket_policy

    # Same length as macOS's per-user /private/var/folders/xx/<31 chars>/T.
    resolved = short_system_temp.resolve()
    user_temp = resolved / ("t" * (56 - len(str(resolved)) - 1))
    user_temp.mkdir()
    assert len(str(user_temp)) == 56
    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(user_temp))

    root = unix_socket_policy.corsia_temp_root_for_bind("6a7e7d87-385a-4c27-a441-50ca3d1a554d")

    assert root == user_temp / "corsia-af-unix" / "17277ddbfd6b"
    assert root.stat().st_mode & 0o777 == 0o700


def test_unusable_bind_root_withholds_only_the_temp_root_grant(
    monkeypatch, tmp_path, short_system_temp, caplog,
):
    from tools import sandbox_config, unix_socket_policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure_sandbox(monkeypatch, sandbox_config, workspace, [])
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
    long_temp = short_system_temp / ("t" * 80)
    long_temp.mkdir()
    monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(long_temp))

    with caplog.at_level("WARNING", logger=unix_socket_policy.__name__):
        add_dirs = sandbox_config.get_sandbox_add_dirs_rw("thread-one")
        rules = unix_socket_policy.unix_socket_sandbox_rules("thread-one")

    assert add_dirs == sandbox_config.get_sandbox_add_dirs_rw()
    assert unix_socket_policy.get_unix_socket_allowed_roots("thread-one") == [workspace.resolve()]
    assert str(long_temp) not in rules
    assert f'(allow network-bind (local unix-socket (subpath "{workspace.resolve()}")))' in rules
    assert "exceeds the macOS socket path limit" in caplog.text
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "socket path limit" in unix_socket_policy.thread_bind_root_withheld_reason(
        "thread-one"
    )


@pytest.mark.parametrize("unsafe_root", ["symlink", "mode", "owner"])
def test_unsafe_bind_root_withholds_grant_and_logs_error_once(
    monkeypatch, tmp_path, caplog, unsafe_root,
):
    from tools import sandbox_config, unix_socket_policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _configure_sandbox(monkeypatch, sandbox_config, workspace, [])
    monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
    temp_parent = tmp_path / "system-temp"
    temp_parent.mkdir()
    root = temp_parent / unix_socket_policy._TEMP_ROOT_NAME
    if unsafe_root == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if unsafe_root == "mode":
            root.chmod(0o755)
        else:
            monkeypatch.setattr(
                unix_socket_policy.os,
                "getuid",
                lambda: root.stat().st_uid + 1,
            )
    monkeypatch.setattr(
        unix_socket_policy.tempfile,
        "gettempdir",
        lambda: str(temp_parent),
    )

    with caplog.at_level("ERROR", logger=unix_socket_policy.__name__):
        add_dirs = sandbox_config.get_sandbox_add_dirs_rw("thread-one")
        rules = unix_socket_policy.unix_socket_sandbox_rules("thread-one")

    assert add_dirs == sandbox_config.get_sandbox_add_dirs_rw()
    assert str(temp_parent) not in rules
    assert "AF_UNIX temp-root grant withheld" in caplog.text
    assert [record.levelname for record in caplog.records] == ["ERROR"]
    assert unix_socket_policy.thread_bind_root_withheld_reason("thread-one")
    assert unix_socket_policy.thread_bind_root_withheld_reason("thread-two") is None


def test_withheld_bind_root_logs_again_per_thread_and_after_recovery(
    monkeypatch, tmp_path, caplog,
):
    from tools import unix_socket_policy

    outcomes = iter(["unsafe", "unsafe", "ok", "unsafe"])

    def fake_root(_thread_id):
        if next(outcomes) == "ok":
            return tmp_path
        raise unix_socket_policy.UnixSocketRootError("root not private")

    monkeypatch.setattr(unix_socket_policy, "corsia_temp_root_for_bind", fake_root)
    with caplog.at_level("ERROR", logger=unix_socket_policy.__name__):
        assert unix_socket_policy.thread_bind_roots("thread-one") == []
        assert unix_socket_policy.thread_bind_roots("thread-two") == []
        assert unix_socket_policy.thread_bind_roots("thread-one") == [tmp_path]
        assert unix_socket_policy.thread_bind_roots("thread-one") == []

    logged_threads = [record.args[0] for record in caplog.records]
    assert logged_threads == ["thread-one", "thread-two", "thread-one"]


def test_pinned_registered_repo_is_read_only(monkeypatch, tmp_path):
    from tools import sandbox_config

    workspace, repo = _registered_external_repo(tmp_path)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [RepoPolicy("p1", "demo", repo, "main", True)],
    )

    assert repo not in sandbox_config.get_allowed_dirs_rw()
    assert repo in sandbox_config.get_allowed_dirs_ro()


def test_explicitly_unpinned_registered_repo_is_read_write(monkeypatch, tmp_path):
    from tools import sandbox_config

    workspace, repo = _registered_external_repo(tmp_path)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [RepoPolicy("p1", "demo", repo, None, False)],
    )

    assert repo in sandbox_config.get_allowed_dirs_rw()
    assert repo not in sandbox_config.get_allowed_dirs_ro()


def test_unpinned_repo_at_custom_project_path_is_read_write(monkeypatch, tmp_path):
    from tools import sandbox_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "custom-project" / "source" / "app"
    (repo / ".git").mkdir(parents=True)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [RepoPolicy("p1", "app", repo.resolve(), None, False)],
    )

    assert repo.resolve() in sandbox_config.get_allowed_dirs_rw()
    assert sandbox_config.is_path_allowed(repo / "app.py")


def test_filesystem_gate_enforces_boundary_without_safehouse(
    monkeypatch, tmp_path
):
    from tools import sandbox_config
    from tools.filesystem_gate import resolve_path

    workspace, _repo = _registered_external_repo(tmp_path)
    outside = tmp_path / "private" / "secret.txt"
    _configure_sandbox(monkeypatch, sandbox_config, workspace, [])
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    with pytest.raises(PermissionError, match="outside the sandbox"):
        resolve_path(str(outside))


def test_filesystem_gate_allows_registered_repo_outside_legacy_allowed_dir(
    monkeypatch, tmp_path
):
    from tools import sandbox_config
    from tools.filesystem_gate import resolve_path

    workspace, repo = _registered_external_repo(tmp_path)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [RepoPolicy("p1", "demo", repo, None, False)],
    )
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    assert resolve_path(str(repo / "app.py"), workspace, workspace) == (
        repo / "app.py"
    ).resolve()


def test_filesystem_gate_rejects_outside_alias_into_workspace(
    monkeypatch, tmp_path
):
    from tools import sandbox_config
    from tools.filesystem_gate import resolve_path

    workspace = tmp_path / "workspace"
    target = workspace / "projects" / "demo" / "shared" / "inside.txt"
    target.parent.mkdir(parents=True)
    target.write_text("workspace-only\n", encoding="utf-8")
    outside_alias = tmp_path / "outside-alias"
    outside_alias.symlink_to(target.parent, target_is_directory=True)
    _configure_sandbox(monkeypatch, sandbox_config, workspace, [])

    with pytest.raises(PermissionError, match="outside the sandbox"):
        resolve_path(str(outside_alias / target.name))


def test_canonical_policy_rows_not_raw_git_links_define_repo_grants(
    tmp_path, monkeypatch
):
    from tools import sandbox_config

    workspace, repo = _registered_external_repo(tmp_path)
    project = workspace / "projects" / "demo"
    db = _policy_db(project, canonical=True)
    loader = getattr(sandbox_config, "_list_authoritative_repo_policies", None)

    assert callable(loader), "authoritative repository-policy loader is missing"
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        lambda: loader(workspace=workspace, db=db),
    )
    assert loader(workspace=workspace, db=db) == []
    assert not sandbox_config.is_path_allowed(repo / "README.md")
    source_alias = workspace / "projects" / "demo" / "source" / "demo"
    assert not sandbox_config.is_path_allowed(source_alias / "README.md")

    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        "protect_base_checkout) VALUES ('p1', 'demo', ?, NULL, 0)",
        (str(repo),),
    )

    assert loader(workspace=workspace, db=db) == [
        RepoPolicy("p1", "demo", repo, None, False)
    ]
    assert sandbox_config.is_path_allowed(repo / "README.md")
    assert sandbox_config.is_path_allowed(source_alias / "README.md")


def test_branch_pin_without_checkout_protection_remains_read_write(
    tmp_path, monkeypatch
):
    from tools import sandbox_config

    workspace, repo = _registered_external_repo(tmp_path)
    subprocess.run(
        ["git", "init", "-b", "dev", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    project = workspace / "projects" / "demo"
    db = _policy_db(project, canonical=True)
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        "protect_base_checkout) VALUES ('p1', 'demo', ?, 'dev', 0)",
        (str(repo),),
    )
    loader = sandbox_config._list_authoritative_repo_policies
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        lambda: loader(workspace=workspace, db=db),
    )

    assert loader(workspace=workspace, db=db) == [
        RepoPolicy("p1", "demo", repo, "dev", False)
    ]
    assert repo in sandbox_config.get_allowed_dirs_rw()
    assert sandbox_config.check_repo_write_path(repo / "README.md") is None


def test_retargeted_source_link_invalidates_canonical_row_authority(
    tmp_path, monkeypatch
):
    from tools import sandbox_config

    workspace, registered = _registered_external_repo(tmp_path)
    unregistered = tmp_path / "code" / "replacement"
    (unregistered / ".git").mkdir(parents=True)
    source_link = workspace / "projects" / "demo" / "source" / "demo"
    source_link.unlink()
    source_link.symlink_to(unregistered, target_is_directory=True)
    db = _policy_db(
        workspace / "projects" / "demo",
        canonical=True,
    )
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        "protect_base_checkout) VALUES ('p1', 'demo', ?, NULL, 0)",
        (str(registered),),
    )
    loader = sandbox_config._list_authoritative_repo_policies
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        lambda: loader(workspace=workspace, db=db),
    )

    stale = loader(workspace=workspace, db=db)
    assert len(stale) == 1
    assert stale[0].source_binding_valid is False
    assert stale[0].enforcement_error
    assert not sandbox_config.is_path_allowed(registered / "README.md")
    assert not sandbox_config.is_path_allowed(unregistered / "README.md")
    assert not sandbox_config.is_path_allowed(source_link / "README.md")
    assert sandbox_config.check_repo_write_path(registered / "README.md")


def test_deleted_source_link_keeps_workspace_repo_write_quarantined(
    tmp_path,
    monkeypatch,
):
    from tools import sandbox_config, shell_guard

    workspace = tmp_path / "Documents"
    project = workspace / "projects" / "demo"
    source = project / "source"
    repo = workspace / "repos" / "demo"
    source.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "dev", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    worktree = repo / ".worktrees" / "feature"
    gitdir = repo / ".git" / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    marker = worktree / ".git"
    marker.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{marker}\n", encoding="utf-8")
    source_link = source / "demo"
    source_link.symlink_to(repo, target_is_directory=True)
    db = _policy_db(project, canonical=True)
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        "protect_base_checkout) VALUES ('p1', 'demo', ?, 'dev', 1)",
        (str(repo.resolve()),),
    )
    loader = sandbox_config._list_authoritative_repo_policies
    policies = lambda: loader(workspace=workspace, db=db)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        policies,
    )

    source_link.unlink()

    stale = policies()
    assert len(stale) == 1
    assert stale[0].source_binding_valid is False
    assert stale[0].enforcement_error
    assert sandbox_config.check_repo_write_path(repo / "tracked.py")
    assert sandbox_config.check_repo_write_path(worktree / "tracked.py")
    assert worktree.resolve() not in sandbox_config.get_allowed_dirs_rw()
    assert not sandbox_config.is_path_allowed(
        repo / "tracked.py",
        for_write=True,
    )
    assert not sandbox_config.is_path_allowed(
        worktree / "tracked.py",
        for_write=True,
    )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: stale,
    )
    profile = shell_guard.pinned_repo_sandbox_profile(
        "git fetch origin",
        str(repo),
        policies=stale,
    )
    assert f'(deny file-write* (subpath "{repo.resolve()}"))' in profile
    assert (
        f'(allow file-read* file-write* (subpath "{worktree.resolve()}"))'
        not in profile
    )
    assert (
        f'(allow file-read* file-write* (subpath "{repo.resolve() / ".git"}"))'
        not in profile
    )
    with pytest.raises(ValueError, match="not enforceable"):
        shell_guard.pinned_repo_sandbox_profile(
            "git fetch origin",
            str(repo),
            controlled_policy=stale[0],
            controlled_operation="fetch",
            policies=stale,
        )


def test_protected_checkout_without_branch_pin_is_read_only(tmp_path, monkeypatch):
    from tools import sandbox_config

    workspace, repo = _registered_external_repo(tmp_path)
    project = workspace / "projects" / "demo"
    db = _policy_db(project, canonical=True)
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        "protect_base_checkout) VALUES ('p1', 'demo', ?, NULL, 1)",
        (str(repo),),
    )
    loader = sandbox_config._list_authoritative_repo_policies
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        lambda: loader(workspace=workspace, db=db),
    )

    assert loader(workspace=workspace, db=db) == [
        RepoPolicy("p1", "demo", repo, None, True)
    ]
    assert repo not in sandbox_config.get_allowed_dirs_rw()
    assert repo in sandbox_config.get_allowed_dirs_ro()
    assert sandbox_config.check_repo_write_path(repo / "README.md") is not None


@pytest.mark.asyncio
async def test_protected_checkout_only_allows_exact_ignored_untracked_file_writes(
    tmp_path,
    monkeypatch,
):
    from tools import sandbox_config
    from tools.filesystem_rw import WriteFileTool

    workspace = tmp_path / "workspace"
    source = workspace / "projects" / "demo" / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    source_alias = source / "demo"
    source_alias.symlink_to(repo, target_is_directory=True)
    (repo / ".gitignore").write_text(".env\ntracked.env\n", encoding="utf-8")
    (repo / "tracked.env").write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitignore"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "tracked.env"],
        check=True,
        capture_output=True,
        text=True,
    )
    policy = RepoPolicy("p1", "demo", repo.resolve(), None, True)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [policy],
    )

    ignored = repo / ".env"
    ignored_alias = source_alias / ".env"
    tracked_ignored = repo / "tracked.env"
    nonignored = repo / "notes.txt"

    assert sandbox_config.is_path_allowed(ignored, for_write=True)
    assert sandbox_config.is_path_allowed(ignored_alias, for_write=True)
    assert sandbox_config.check_repo_write_path(ignored) is None
    assert not sandbox_config.is_path_allowed(tracked_ignored, for_write=True)
    assert sandbox_config.check_repo_write_path(tracked_ignored) is not None
    assert not sandbox_config.is_path_allowed(nonignored, for_write=True)
    assert sandbox_config.check_repo_write_path(nonignored) is not None
    assert not sandbox_config.is_path_allowed(repo, for_write=True)
    assert sandbox_config.check_repo_write_path(repo) is not None

    written = await WriteFileTool().execute(
        path=str(ignored_alias),
        content="secret\n",
    )
    blocked = await WriteFileTool().execute(
        path=str(nonignored),
        content="blocked\n",
    )

    assert written.startswith("Successfully wrote")
    assert ignored.read_text(encoding="utf-8") == "secret\n"
    assert "protected base checkout" in blocked
    assert not nonignored.exists()


def test_repo_worktree_root_requires_nonsymlink_reciprocal_gitdir_proof(
    tmp_path,
):
    from tools import sandbox_config

    repo = tmp_path / "repo"
    worktree = repo / ".worktrees" / "feature"
    gitdir = repo / ".git" / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    marker = worktree / ".git"
    marker.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    assert sandbox_config.repo_worktree_root(worktree, repo) is None

    (gitdir / "gitdir").write_text(f"{marker}\n", encoding="utf-8")
    assert sandbox_config.repo_worktree_root(worktree, repo) == worktree.resolve()

    marker.unlink()
    marker.symlink_to(gitdir, target_is_directory=True)
    assert sandbox_config.repo_worktree_root(worktree, repo) is None


def test_repository_enforcement_error_blocks_writes_without_guessing(
    tmp_path,
    monkeypatch,
):
    from tools import sandbox_config

    workspace, repo = _registered_external_repo(tmp_path)
    policy = RepoPolicy(
        "p1",
        "demo",
        repo,
        "main",
        False,
        "current branch 'release' differs from pinned branch 'main'",
    )
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [policy],
    )

    error = sandbox_config.check_repo_write_path(repo / "README.md")

    assert error is not None
    assert "enforcement failed" in error
    assert "release" in error
    assert not sandbox_config.is_path_allowed(
        repo / "README.md",
        for_write=True,
    )


def test_pin_only_file_tools_allow_content_but_not_git_control_paths(
    tmp_path,
    monkeypatch,
):
    from tools import sandbox_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    policy = RepoPolicy("p1", "demo", repo.resolve(), "main", False)
    _configure_sandbox(
        monkeypatch,
        sandbox_config,
        workspace,
        [policy],
    )

    assert sandbox_config.is_path_allowed(
        repo / "README.md",
        for_write=True,
    )
    error = sandbox_config.check_repo_write_path(repo / ".git" / "HEAD")
    assert error is not None
    assert "Git control metadata" in error
    assert not sandbox_config.is_path_allowed(
        repo / ".git" / "HEAD",
        for_write=True,
    )


def _linked_worktree(repo: Path, name: str) -> Path:
    worktree = repo / ".worktrees" / name
    gitdir = repo / ".git" / "worktrees" / name
    worktree.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    marker = worktree / ".git"
    marker.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{marker}\n", encoding="utf-8")
    return worktree.resolve()


def test_profile_worktree_grants_match_legacy_add_dirs(
    tmp_path, monkeypatch, short_system_temp,
):
    """Moving worktree grants off ``--add-dirs`` keeps the same RW set."""
    import re

    from tools import sandbox_config, shell_guard, unix_socket_policy

    monkeypatch.setattr(
        unix_socket_policy.tempfile, "gettempdir", lambda: str(short_system_temp),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repos = {}
    for name in ("protected", "quarantined", "writable"):
        repo = tmp_path / name
        subprocess.run(
            ["git", "init", "-b", "dev", str(repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        repos[name] = repo.resolve()
    granted = [
        _linked_worktree(repos["protected"], name)
        for name in ("a", "b", "c")
    ]
    # Unverified directories under .worktrees are never granted.
    (repos["protected"] / ".worktrees" / "rogue").mkdir()
    quarantined = _linked_worktree(repos["quarantined"], "q")
    policies = [
        RepoPolicy("p1", "protected", repos["protected"], "dev", True),
        RepoPolicy(
            "p1", "quarantined", repos["quarantined"], "dev", True,
            enforcement_error="stale binding",
        ),
        RepoPolicy("p1", "writable", repos["writable"], None, False),
    ]
    _configure_sandbox(monkeypatch, sandbox_config, workspace, policies)
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: policies)

    legacy_add_dirs = set(sandbox_config.get_allowed_dirs_rw())
    add_dirs = set(sandbox_config.get_sandbox_add_dirs_rw("thread-one"))
    profile = shell_guard.pinned_repo_sandbox_profile(
        "true", str(workspace), policies=policies,
    )
    profile_grants = {
        Path(path)
        for path in re.findall(
            r'^\(allow file-read\* file-write\* \(subpath "([^"]+)"\)\)$',
            profile,
            flags=re.MULTILINE,
        )
    }

    assert profile_grants == set(granted)
    assert add_dirs.isdisjoint(profile_grants)
    assert add_dirs | profile_grants == legacy_add_dirs | {
        unix_socket_policy.corsia_temp_root_for_bind("thread-one")
    }
    assert quarantined not in legacy_add_dirs | profile_grants
    assert repos["protected"] / ".worktrees" / "rogue" not in profile_grants
    lines = profile.splitlines()
    base_deny = lines.index(
        f'(deny file-write* (subpath "{repos["protected"]}"))'
    )
    for worktree in granted:
        grant = lines.index(
            f'(allow file-read* file-write* (subpath "{worktree}"))'
        )
        marker_deny = lines.index(
            f'(deny file-write* (literal "{worktree / ".git"}"))'
        )
        assert base_deny < grant < marker_deny

    safehouse = tmp_path / "bin" / "safehouse"
    safehouse.parent.mkdir()
    safehouse.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: str(safehouse))
    argv = shell_guard.safehouse_argv(
        "true", workspace, tmp_path / "p.sb", thread_id="thread-one",
    )
    (rw_arg,) = [part for part in argv if part.startswith("--add-dirs=")]
    assert {
        Path(path) for path in rw_arg.removeprefix("--add-dirs=").split(":")
    } == add_dirs


def _old_is_path_allowed(sandbox_config, path: Path, *, for_write: bool = False) -> bool:
    """Pre-optimization oracle for authorization-equivalence coverage."""
    resolved = path.expanduser().resolve(strict=False)
    policies = sandbox_config._list_authoritative_repo_policies()
    exception_root: Path | None = None
    if for_write:
        error, exception_root = sandbox_config._repo_write_check(resolved, policies)
        if error:
            return False

    allowed_dirs = sandbox_config._allowed_dirs_rw(policies)
    if exception_root is not None:
        allowed_dirs.append(exception_root)
    if not for_write:
        allowed_dirs += [
            *sandbox_config._STATIC_ALLOWED_RO,
            *(
                policy.path
                for policy in policies
                if (
                    policy.source_binding_valid
                    and (
                        policy.protect_base_checkout
                        or policy.enforcement_error
                    )
                )
            ),
        ]

    lexical = sandbox_config._lexical_absolute(path)
    lexical_allowed = any(
        sandbox_config._is_within(
            lexical, sandbox_config._lexical_absolute(allowed)
        )
        or sandbox_config._is_within(
            lexical, allowed.expanduser().resolve(strict=False)
        )
        for allowed in allowed_dirs
    )
    resolved_allowed = any(
        sandbox_config._is_within(
            resolved, allowed.expanduser().resolve(strict=False)
        )
        for allowed in allowed_dirs
    )
    return lexical_allowed and resolved_allowed


def test_is_path_allowed_matches_pre_optimization_oracle(
    tmp_path, monkeypatch
):
    """The narrow worktree lookup preserves every old authorization result."""
    from tools import sandbox_config

    workspace = tmp_path / "workspace"
    static_ro = tmp_path / "static-ro"
    workspace.mkdir()
    static_ro.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / ".git").mkdir()
    verified = _linked_worktree(protected, "verified")
    destination = _linked_worktree(protected, "destination")
    (verified / "enters-worktree").symlink_to(destination, target_is_directory=True)
    (protected / ".worktrees" / "missing").mkdir()
    marker_link = protected / ".worktrees" / "marker-link"
    marker_link.mkdir()
    (marker_link / ".git").symlink_to(verified / ".git")
    malformed = protected / ".worktrees" / "malformed"
    malformed.mkdir()
    (malformed / ".git").write_text("not a gitdir marker\n", encoding="utf-8")
    reciprocal_bad = protected / ".worktrees" / "reciprocal-bad"
    reciprocal_bad_gitdir = protected / ".git" / "worktrees" / "reciprocal-bad"
    reciprocal_bad.mkdir()
    reciprocal_bad_gitdir.mkdir(parents=True)
    (reciprocal_bad / ".git").write_text(
        f"gitdir: {reciprocal_bad_gitdir}\n", encoding="utf-8"
    )
    (reciprocal_bad_gitdir / "gitdir").write_text(
        f"{verified / '.git'}\n", encoding="utf-8"
    )
    (protected / ".worktrees" / "inside-alias").symlink_to(
        verified, target_is_directory=True
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (protected / ".worktrees" / "outside-alias").symlink_to(
        outside, target_is_directory=True
    )
    (verified / "escapes").symlink_to(outside, target_is_directory=True)
    (outside / "enters").symlink_to(verified, target_is_directory=True)

    writable = tmp_path / "writable"
    writable.mkdir()
    (writable / ".git").mkdir()
    quarantined = tmp_path / "quarantined"
    quarantined.mkdir()
    (quarantined / ".git").mkdir()
    policies = [
        RepoPolicy("p1", "protected", protected, "dev", True),
        RepoPolicy("p1", "writable", writable, None, False),
        RepoPolicy(
            "p1", "quarantined", quarantined, "dev", True,
            enforcement_error="binding failed",
        ),
    ]
    _configure_sandbox(monkeypatch, sandbox_config, workspace, policies)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [static_ro])

    ignored = protected / ".gitignore"
    ignored.write_text("exception.tmp\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(protected), "init", "-b", "dev"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(protected), "add", ".gitignore"],
        check=True,
        capture_output=True,
        text=True,
    )
    exception = protected / "exception.tmp"

    matrix = {
        "verified worktree": verified / "present.py",
        "missing worktree marker": protected / ".worktrees" / "missing" / "x",
        "symlinked worktree marker": marker_link / "x",
        "malformed worktree marker": malformed / "x",
        "reciprocal gitdir mismatch": reciprocal_bad / "x",
        "symlinked entry inside repo": protected / ".worktrees" / "inside-alias" / "x",
        "symlinked entry outside repo": protected / ".worktrees" / "outside-alias" / "x",
        "lexical worktree resolving outside": verified / "escapes" / "x",
        "lexical worktree resolving into another": verified / "enters-worktree" / "x",
        "lexical outside resolving into worktree": outside / "enters" / "x",
        "protected base checkout": protected / "README.md",
        "protected git directory": protected / ".git" / "HEAD",
        "static read-write root": workspace / "notes.md",
        "static read-only root": static_ro / "notes.md",
        "unprotected repo": writable / "README.md",
        "enforcement-error repo": quarantined / "README.md",
        "protected exception root": exception,
        "nonexistent protected worktree path": verified / "missing" / "file",
        "nonexistent unprotected path": writable / "missing" / "file",
    }

    for for_write in (False, True):
        for name, candidate in matrix.items():
            expected = _old_is_path_allowed(
                sandbox_config, candidate, for_write=for_write
            )
            actual = sandbox_config.is_path_allowed(candidate, for_write=for_write)
            assert actual == expected, f"{name}; for_write={for_write}"
