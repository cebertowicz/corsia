from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tools import shell_guard  # noqa: E402
from infra.project_repo_policy import RepoPolicy  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT


_RECOVERY_FETCH = "fetches the configured remote"
_RECOVERY_WORKTREE = "`worktree_create`"


def _set_pinned_repo(monkeypatch, tmp_path):
    repo = tmp_path / "workspace" / "projects" / "app" / "source" / "app"
    worktree = repo / ".worktrees" / "feature"
    gitdir = repo / ".git" / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n",
        encoding="utf-8",
    )
    marker = worktree / ".git"
    marker.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{marker}\n", encoding="utf-8")
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_rw", lambda: [tmp_path])
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id: [tmp_path])
    return repo, worktree


def _assert_verified_worktree_grant_keeps_controls_denied(
    profile: str,
    repo: Path,
    worktree: Path,
) -> None:
    """Prove the ordinary Git grant does not reopen shared/base controls."""
    repo = repo.resolve()
    worktree = worktree.resolve()
    git_allow = f'(allow file-read* file-write* (subpath "{repo / ".git"}"))'
    assert git_allow in profile
    assert f'(deny file-write* (subpath "{repo}"))' in profile
    for path in (
        repo / ".git" / "HEAD",
        repo / ".git" / "index",
        repo / ".git" / "logs" / "HEAD",
        repo / ".git" / "refs" / "heads" / "main",
        repo / ".git" / "refs" / "heads" / "main.lock",
        repo / ".git" / "logs" / "refs" / "heads" / "main",
        repo / ".git" / "logs" / "refs" / "heads" / "main.lock",
        repo / ".git" / "config",
        repo / ".git" / "config.lock",
        repo / ".git" / "packed-refs",
        repo / ".git" / "packed-refs.lock",
    ):
        deny = f'(deny file-write* (literal "{path}"))'
        assert profile.index(deny) > profile.index(git_allow)
    hooks_deny = f'(deny file-write* (subpath "{repo / ".git" / "hooks"}"))'
    marker_deny = f'(deny file-write* (literal "{worktree / ".git"}"))'
    assert profile.index(hooks_deny) > profile.index(git_allow)
    assert profile.index(marker_deny) > profile.index(git_allow)


@pytest.mark.parametrize(
    "command_template",
    [
        "git switch feature",
        "git checkout feature",
        "git checkout -- src/app.py",
        "git branch new-branch",
        "git branch -m renamed",
        "git branch -D old-branch",
        "git symbolic-ref HEAD refs/heads/renamed",
        "git symbolic-ref --delete HEAD",
        "git -C {repo} switch feature",
    ],
)
def test_pinned_base_rejects_direct_branch_mutation_with_recovery(
    command_template,
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)
    command = command_template.format(repo=repo)

    error = shell_guard.check_worktree_targets(command, str(repo))

    assert error is not None
    assert "branch-pinned" in error
    assert _RECOVERY_FETCH in error
    assert _RECOVERY_WORKTREE in error


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git branch",
        "git branch --show-current",
        "git branch --list feature",
        "git symbolic-ref HEAD",
    ],
)
def test_pinned_base_allows_direct_read_only_git_queries(
    command,
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)

    assert shell_guard.check_worktree_targets(command, str(repo)) is None


@pytest.mark.parametrize(
    "command",
    [
        "git switch other",
        "git checkout other",
        "git branch new-branch",
        "git symbolic-ref HEAD refs/heads/other",
    ],
)
def test_verified_worktree_allows_direct_branch_mutation(
    command,
    monkeypatch,
    tmp_path,
):
    _, worktree = _set_pinned_repo(monkeypatch, tmp_path)

    assert shell_guard.check_worktree_targets(command, str(worktree)) is None


def test_unpinned_repo_allows_direct_branch_mutation(monkeypatch, tmp_path):
    repo = tmp_path / "single-checkout"
    repo.mkdir()
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), None, False)],
    )

    assert shell_guard.check_worktree_targets("git switch feature", str(repo)) is None


@pytest.mark.parametrize(
    ("pinned_branch", "protected", "base_denied", "head_denied"),
    [
        (None, False, False, False),
        ("main", False, False, True),
        (None, True, True, True),
        ("main", True, True, True),
    ],
)
def test_repository_profile_keeps_pin_and_checkout_protection_independent(
    pinned_branch,
    protected,
    base_denied,
    head_denied,
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [
            RepoPolicy(
                "p1",
                "app",
                repo.resolve(),
                pinned_branch,
                protected,
            )
        ],
    )

    profile = shell_guard.pinned_repo_sandbox_profile("printf ok", str(repo))

    base_deny = f'(deny file-write* (subpath "{repo.resolve()}"))'
    head_deny = (
        f'(deny file-write* (literal "{repo.resolve() / ".git" / "HEAD"}"))'
    )
    assert (base_deny in profile) is base_denied
    assert (head_deny in profile) is head_denied


def test_protected_unpinned_base_gets_only_literal_configured_fetch_exception(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    policy = RepoPolicy("p1", "app", repo.resolve(), None, True)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )
    monkeypatch.setattr(
        shell_guard,
        "_configured_remote_names",
        lambda _repo: {"origin"},
    )

    literal = shell_guard.pinned_repo_sandbox_profile(
        "git fetch origin",
        str(repo),
    )
    composed = shell_guard.pinned_repo_sandbox_profile(
        "git fetch origin && printf nope",
        str(repo),
    )
    controlled_merge = shell_guard.pinned_repo_sandbox_profile(
        "",
        str(repo),
        controlled_policy=policy,
        controlled_operation="merge",
        policies=[policy],
    )
    git_allow = (
        f'(allow file-read* file-write* (subpath "{repo.resolve() / ".git"}"))'
    )
    repo_allow = (
        f'(allow file-read* file-write* (subpath "{repo.resolve()}"))'
    )
    head_deny = (
        f'(deny file-write* (literal "{repo.resolve() / ".git" / "HEAD"}"))'
    )

    assert git_allow in literal
    assert git_allow not in composed
    assert f'(deny file-write* (subpath "{repo.resolve()}"))' in literal
    assert controlled_merge.index(head_deny) > controlled_merge.index(repo_allow)


def test_controlled_fast_forward_classifier_accepts_only_exact_base_command(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = RepoPolicy("p1", "app", repo.resolve(), "main", True)
    policies = [policy]
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: policies,
    )

    assert (
        shell_guard.controlled_fast_forward_policy(
            "git pull --ff-only",
            str(repo),
            policies=policies,
        )
        == policy
    )
    for command in (
        "git pull --ff-only origin main",
        "git pull --ff-only && printf composed",
        "/usr/bin/git pull --ff-only",
        f"git -C {repo} pull --ff-only",
        "bash -c 'git pull --ff-only'",
        "git pull $FLAGS",
    ):
        assert (
            shell_guard.controlled_fast_forward_policy(
                command,
                str(repo),
                policies=policies,
            )
            is None
        )
    for command in (
        f"git -C {repo} pull --ff-only",
        "/usr/bin/git pull --ff-only",
        "git pull --ff-only && printf composed",
    ):
        assert shell_guard.check_worktree_targets(command, str(repo)) is not None


def test_repository_enforcement_error_gets_no_shell_write_exception(
    monkeypatch,
    tmp_path,
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    policy = RepoPolicy(
        "p1",
        "app",
        repo.resolve(),
        "main",
        False,
        "current branch mismatch",
    )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [policy],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "git fetch origin",
        str(repo),
    )

    assert f'(deny file-write* (subpath "{repo.resolve()}"))' in profile
    assert (
        f'(allow file-read* file-write* (subpath "{repo.resolve() / ".git"}"))'
        not in profile
    )
    assert (
        shell_guard.controlled_fast_forward_policy(
            "git pull --ff-only",
            str(repo),
            policies=[policy],
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "cd source/app && git switch feature",
        "bash -c 'git switch feature'",
        "git -c alias.go='switch feature' go",
        "/usr/bin/git switch feature",
        "GIT_DIR=.git git switch feature",
        "git fetch $REMOTE",
    ],
)
def test_composed_git_forms_use_verified_worktree_grant_but_protect_controls(
    command,
    monkeypatch,
    tmp_path,
):
    repo, worktree = _set_pinned_repo(monkeypatch, tmp_path)

    error = shell_guard.check_worktree_targets(command, str(repo))
    if command.startswith("/usr/bin/git"):
        assert error is not None
    else:
        assert error is None
    profile = shell_guard.pinned_repo_sandbox_profile(
        command,
        str(repo),
        grant_policies=[RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    _assert_verified_worktree_grant_keeps_controls_denied(
        profile,
        repo,
        worktree,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git worktree list --porcelain | wc -l",
        "git worktree list > inventory.txt",
    ],
)
def test_composed_read_only_worktree_queries_remain_available(
    command,
    monkeypatch,
    tmp_path,
):
    repo, worktree = _set_pinned_repo(monkeypatch, tmp_path)

    assert shell_guard.check_worktree_targets(command, str(repo)) is None
    profile = shell_guard.pinned_repo_sandbox_profile(
        command,
        str(repo),
        grant_policies=[RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    _assert_verified_worktree_grant_keeps_controls_denied(
        profile,
        repo,
        worktree,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git fetch",
        "git fetch origin",
        "git pull --ff-only",
    ],
)
def test_pinned_base_allows_literal_fetch_and_fast_forward_pull(
    command,
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shell_guard,
        "_configured_remote_names",
        lambda _repo: {"origin"},
    )

    assert shell_guard.check_worktree_targets(command, str(repo)) is None


@pytest.mark.parametrize(
    "command",
    [
        "git fetch upstream",
        "git fetch origin dev",
        "git pull",
        "git pull origin dev",
        "git pull --rebase",
    ],
)
def test_pinned_base_rejects_broad_fetch_and_pull(
    command,
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shell_guard,
        "_configured_remote_names",
        lambda _repo: {"origin"},
    )

    assert shell_guard.check_worktree_targets(command, str(repo)) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add --no-track -b new .worktrees/new origin/dev",
        "git worktree add .worktrees/new --no-track -b new origin/dev",
    ],
)
def test_protected_base_requires_fresh_worktree_tool_for_new_branch(
    command,
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)

    error = shell_guard.check_worktree_targets(command, str(repo))

    assert error is not None
    assert "worktree_create" in error
    assert "fetch" in error


def test_unprotected_pinned_base_keeps_raw_worktree_creation(
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [RepoPolicy("p1", "app", repo.resolve(), "main", False)],
    )

    assert (
        shell_guard.check_worktree_targets(
            "git worktree add --no-track -b new .worktrees/new origin/dev",
            str(repo),
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add ../outside -b new origin/dev",
        "git worktree move .worktrees/feature ../outside",
        "git worktree lock ../outside",
        "git worktree remove ../outside",
    ],
)
def test_pinned_repo_rejects_worktree_paths_outside_its_container(
    command,
    monkeypatch,
    tmp_path,
):
    repo, _ = _set_pinned_repo(monkeypatch, tmp_path)

    error = shell_guard.check_worktree_targets(command, str(repo))

    assert error is not None
    assert ".worktrees" in error


@pytest.mark.parametrize("location", ["worktree", "base-subdir"])
def test_worktree_mutation_requires_exact_owning_base(
    location,
    monkeypatch,
    tmp_path,
):
    repo, worktree = _set_pinned_repo(monkeypatch, tmp_path)
    base_subdir = repo / "src"
    base_subdir.mkdir()
    cwd = worktree if location == "worktree" else base_subdir
    command = "git worktree add .worktrees/nested -b nested HEAD"

    error = shell_guard.check_worktree_targets(command, str(cwd))
    profile = shell_guard.pinned_repo_sandbox_profile(command, str(cwd))

    assert error is not None
    assert "owning repository" in error
    assert f'(allow file-read* file-write* (subpath "{repo / ".git"}"))' not in profile


@pytest.mark.parametrize(
    ("version_output", "expected_error"),
    [
        ("Agent Safehouse 0.11.1", None),
        ("Agent Safehouse 1.0.0", None),
        ("Agent Safehouse 0.11.0", "0.11.1 or newer"),
        ("unknown", "could not determine"),
    ],
)
def test_safehouse_version_gate(version_output, expected_error, monkeypatch):
    class Result:
        returncode = 0
        stdout = version_output
        stderr = ""

    monkeypatch.setattr(shell_guard.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(
        shell_guard,
        "_safehouse_executable",
        lambda: Path("/usr/local/bin/safehouse"),
    )

    error = shell_guard.safehouse_version_error()

    if expected_error is None:
        assert error is None
    else:
        assert expected_error in error


def test_safehouse_version_gate_fails_closed_when_probe_fails(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "broken"

    monkeypatch.setattr(shell_guard.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(
        shell_guard,
        "_safehouse_executable",
        lambda: Path("/usr/local/bin/safehouse"),
    )

    assert "could not verify" in shell_guard.safehouse_version_error()


@pytest.mark.parametrize(
    "command",
    [
        f"agent-browser open http://localhost:{DEFAULT_BACKEND_PORT}",
        "/opt/homebrew/bin/agent-browser eval 'document.body.innerText'",
        "npx agent-browser click @e1",
    ],
)
def test_shell_allows_direct_agent_browser(command):
    assert shell_guard.check_command_safety(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "pkill -f my-dev-server",
        "pkill python",
        "killall node",
        "kill -1 1234",
        "kill 0",
    ],
)
def test_shell_allows_in_jail_process_management(command):
    assert shell_guard.check_command_safety(command) is None


@pytest.mark.asyncio
async def test_shell_refuses_an_unsupported_safehouse(monkeypatch, tmp_path):
    from core.turn_core import TurnIdentity
    from tools import host_access_gate, shell

    async def no_permit(**_kwargs):
        return None

    monkeypatch.setattr(
        host_access_gate,
        "consume_current_action_permit",
        no_permit,
    )
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(
        shell,
        "safehouse_version_error",
        lambda: "Agent Safehouse 0.11.1 or newer is required (found 0.9.0).",
        raising=False,
    )
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda: [])

    result = await shell.BashTool(working_dir=str(tmp_path)).execute(
        "printf ok",
        _thread_id="thread-1",
        _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )

    assert result == (
        "Error: Agent Safehouse 0.11.1 or newer is required (found 0.9.0)."
    )


@pytest.mark.parametrize(
    "chained_command",
    [
        "git add -A && git commit -m 'wip'",
        "git add file.py; git commit --amend --no-edit",
        "git rev-parse HEAD | tee /tmp/head.txt",
        "make test",
        "./run.sh && git commit -am build",
    ],
)
def test_worktree_grant_is_inventory_keyed_and_shape_equivalent(
    chained_command,
    monkeypatch,
    tmp_path,
):
    """Verified inventory mints the same scoped grant for nonliteral commands."""
    repo, worktree = _set_pinned_repo(monkeypatch, tmp_path)

    literal = shell_guard.pinned_repo_sandbox_profile("git status", str(worktree))
    chained = shell_guard.pinned_repo_sandbox_profile(
        chained_command,
        str(worktree),
        grant_policies=[RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )

    git_allow = (
        f'(allow file-read* file-write* (subpath "{repo.resolve() / ".git"}"))'
    )
    head_deny = (
        f'(deny file-write* (literal "{repo.resolve() / ".git" / "HEAD"}"))'
    )
    # Profile-equivalence proves the new path grants nothing a literal
    # `git status` in the same cwd does not already grant today.
    assert chained == literal
    assert git_allow in chained
    # Shared branch identity stays denied — the grant only opens `.git` writes
    # that the appended control denies do not cover.
    assert head_deny in chained


def test_base_checkout_chained_git_uses_verified_grant_and_keeps_teaching(
    monkeypatch,
    tmp_path,
):
    """Verified inventory grants Git while worktree-add remains base-denied."""
    repo, worktree = _set_pinned_repo(monkeypatch, tmp_path)
    monkeypatch.setattr(
        shell_guard,
        "_configured_remote_names",
        lambda _repo: {"origin"},
    )
    command = (
        "git fetch origin && git worktree add --no-track -b x "
        ".worktrees/x origin/main"
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        command,
        str(repo),
        grant_policies=[RepoPolicy("p1", "app", repo.resolve(), "main", True)],
    )
    _assert_verified_worktree_grant_keeps_controls_denied(
        profile,
        repo,
        worktree,
    )
    assert (
        f'(allow file-read* file-write* '
        f'(subpath "{repo.resolve() / ".worktrees"}"))'
        not in profile
    )

    teaching = shell_guard.chained_base_git_denial_guidance(command, str(repo))
    assert teaching is not None
    assert "one command at a time" in teaching
    # The residual guidance never fires for a single literal Git command.
    assert (
        shell_guard.chained_base_git_denial_guidance("git fetch origin", str(repo))
        is None
    )


def test_chained_base_git_teaching_scoped_to_grantable_and_base_only(
    monkeypatch,
    tmp_path,
):
    """The teaching only fires where unchaining actually helps: a base checkout
    with a grantable segment — never a worktree cwd, never a non-grantable
    composed form."""
    repo, worktree = _set_pinned_repo(monkeypatch, tmp_path)

    # Worktree cwds just work now; no teaching.
    assert (
        shell_guard.chained_base_git_denial_guidance(
            "git add -A && git commit -m x",
            str(worktree),
        )
        is None
    )
    # A composed base form with no base-grantable segment is a genuine
    # protected-base denial (use a worktree), not a chaining mistake.
    assert (
        shell_guard.chained_base_git_denial_guidance(
            "git add -A && git commit -m x",
            str(repo),
        )
        is None
    )
