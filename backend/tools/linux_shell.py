"""Protected Linux shell wiring; Mac renderers remain independent."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from tools.base_git_paths import base_git_control_paths, base_git_identity_paths, base_branch_selector_paths
from tools.host_path_policy import current_protected_host_path_policy, GENERATED_WORKSPACE_PROJECTIONS
from tools.linux_sandbox import build_protected_argv
from tools.sandbox_config import get_sandbox_add_dirs_rw, get_allowed_dirs_ro, list_repo_worktrees
from tools.shell_environment import agent_account_ro_dirs, agent_tool_ro_dirs, shell_env_pass_names


def _repo_mounts(policies, controlled_policy=None, controlled_operation=None):
    """Protect the common-dir root, then grant existing metadata subdirectories.

    New common-dir files cannot be created, including absent protected controls.
    Ordinary unpinned/unprotected repos keep their ordinary workspace grant.
    """
    ro, rw, overlays = [], [], []
    for policy in policies:
        repo = policy.path.resolve()
        if not (policy.branch_pinned or policy.protect_base_checkout or policy.enforcement_error
                or not policy.source_binding_valid):
            continue
        if policy.protect_base_checkout or policy.enforcement_error or not policy.source_binding_valid:
            ro.append(repo)
        if not policy.source_binding_valid or policy.enforcement_error:
            continue
        worktrees = list_repo_worktrees(repo)
        rw.extend(worktrees)
        git = repo / '.git'
        if not git.is_dir():
            overlays.append(git)
            continue
        ro.append(git)
        for name in ('objects', 'refs', 'logs'):
            if (git / name).is_dir():
                rw.append(git / name)
        for worktree in worktrees:
            marker = worktree / '.git'
            overlays.append(marker)
            try:
                target = Path(marker.read_text().strip().removeprefix('gitdir: '))
                target = (worktree / target).resolve() if not target.is_absolute() else target.resolve()
            except OSError:
                continue
            if target.is_dir() and target.is_relative_to(git / 'worktrees'):
                rw.append(target)
        # Shared controls stay ro even if an add-dir covers the common dir.
        overlays.extend(base_git_control_paths(repo))
        if policy.protect_base_checkout:
            overlays.extend(base_git_identity_paths(repo))
            # Missing identities use the renderer's generic absence boundary.
        else:
            overlays.extend(base_branch_selector_paths(repo))
        overlays.append(git / 'hooks')
        if controlled_policy is not None and repo == controlled_policy.path.resolve():
            # A base merge needs root-level index.lock and cannot use this
            # structural mount plan without widening absent-control writes.
            if controlled_operation == 'merge':
                raise ValueError('Linux protected base merge is unavailable: common Git directory is read-only')
    # The approved lock-file delta blocks final rename to the ro data file.
    # A missing data file still uses the generic absent-path boundary.
    overlays = [path for path in overlays if not (
        path.suffix == '.lock' and not path.exists() and Path(str(path)[:-5]).exists()
    )]
    return ro, rw, overlays


def shell_argv(command: str, cwd: Path, _profile=None, *, thread_id: str, policies: list[Any],
               env: dict[str, str], agent_account=None, controlled_policy=None,
               controlled_operation=None, extra_hidden_roots=()) -> list[str]:
    from connections import github_agent
    from infra.config import WORKSPACE
    from infra.project_repo_policy import list_unsupported_pinned_repos
    path_policy = current_protected_host_path_policy()
    repo_ro, repo_rw, overlays = _repo_mounts(policies, controlled_policy, controlled_operation)
    rw = [*get_sandbox_add_dirs_rw(thread_id, policies=policies), *repo_rw]
    ro = [*get_allowed_dirs_ro(policies=policies), *agent_account_ro_dirs(agent_account),
          *agent_tool_ro_dirs(), *repo_ro]
    overlays.extend(WORKSPACE / path for path in GENERATED_WORKSPACE_PROJECTIONS)
    for unsupported in list_unsupported_pinned_repos(policies):
        overlays.extend(unsupported.metadata_roots)
    hidden_account = github_agent.default_home_deny(
        github_agent.view() if agent_account is None else agent_account)
    names = set(shell_env_pass_names().split(',')) | {
        'HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM',
        'COLORTERM', 'SSH_AUTH_SOCK', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
        'CARGO_HOME', 'RUSTUP_HOME', 'GOPATH', 'GOCACHE', 'UV_CACHE_DIR',
    }
    passed = {name: value for name, value in env.items() if name in names}
    passed['TMPDIR'] = '/tmp'
    passed.setdefault('HOME', str(path_policy.home))
    passed.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
    executable = shutil.which('bwrap')
    if executable is None:
        from tools.linux_sandbox_probe import sandbox_error
        raise RuntimeError(sandbox_error())
    return build_protected_argv(command, Path(cwd), path_policy=path_policy,
                                rw_roots=rw, ro_roots=ro, readonly_overlays=overlays,
                                hidden_roots=[*extra_hidden_roots, *([hidden_account] if hidden_account else [])],
                                env=passed, thread_id=thread_id, executable=executable)
