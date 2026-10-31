from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest

from tests.linux_sandbox_test_support import linux_sandbox_root
from tools.workspace_git_paths import workspace_git_paths, workspace_git_rules


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True, timeout=30)


def init(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')


def test_guidance_status_ignores_nested_repository(tmp_path):
    from infra.guidance_git import _changed

    repo = tmp_path / 'workspace'
    init(repo)
    nested = repo / 'skills' / 'demo' / 'evals' / 'case'
    init(nested)
    (nested / 'tracked').write_text('one')
    git(nested, 'add', 'tracked')
    git(nested, 'commit', '-qm', 'nested')
    git(repo, 'add', 'skills')
    git(repo, 'commit', '-qm', 'root')
    marker = tmp_path / 'ran'
    script = tmp_path / 'monitor'
    script.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\n')
    script.chmod(0o755)
    git(nested, 'config', 'core.fsmonitor', str(script))
    (nested / 'tracked').write_text('two')
    git(repo, 'status', '--porcelain=v1', '--untracked-files=all', '--no-renames', '--', 'skills')
    assert marker.exists(), 'control did not exercise nested Git configuration'
    marker.unlink()
    git(repo, '-c', 'core.fsmonitor=false', 'status', '--porcelain=v1', '--untracked-files=all', '--no-renames', '--', 'skills')
    assert not marker.exists()
    _changed(repo, ['skills'])
    assert not marker.exists()


def test_nested_git_clean_filter_with_fsmonitor_disabled(tmp_path):
    repo = tmp_path / 'workspace'
    init(repo)
    nested = repo / 'skills' / 'demo' / 'evals' / 'case'
    init(nested)
    marker = tmp_path / 'filter-ran'
    script = tmp_path / 'filter'
    script.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\ncat\n')
    script.chmod(0o755)
    git(nested, 'config', 'filter.demo.clean', str(script))
    (nested / '.gitattributes').write_text('tracked filter=demo\n')
    (nested / 'tracked').write_text('one')
    git(nested, 'add', '.')
    git(nested, 'commit', '-qm', 'nested')
    git(repo, 'add', 'skills')
    git(repo, 'commit', '-qm', 'root')
    (nested / 'tracked').write_text('two')
    marker.unlink(missing_ok=True)
    git(nested, '-c', 'core.fsmonitor=false', 'add', 'tracked')
    assert marker.exists()


def test_reaper_keeps_gitlinks_without_running_nested_git(tmp_path):
    from infra.worktree_reaper_checks import _status_verdict
    from infra.worktree_reaper_probe import status_skip_reason, worktree_git_env
    from infra.worktree_reaper_salvage import SalvageError, _selected

    repo = tmp_path / 'workspace'
    init(repo)
    nested = repo / 'nested'
    init(nested)
    (nested / 'tracked').write_text('one')
    git(nested, 'add', 'tracked')
    git(nested, 'commit', '-qm', 'nested')
    git(repo, 'add', 'nested')
    git(repo, 'commit', '-qm', 'root')
    marker = tmp_path / 'ran'
    script = tmp_path / 'monitor'
    script.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\n')
    script.chmod(0o755)
    git(nested, 'config', 'core.fsmonitor', str(script))
    (nested / 'tracked').write_text('changed')
    git_env = worktree_git_env(repo, repo / '.git', repo)
    assert status_skip_reason(repo, git_env) == 'submodule'
    assert _status_verdict(None, None, repo, 'submodule', 0, 0) == 'submodule'
    with pytest.raises(SalvageError, match='submodule'):
        _selected(repo, git_env)
    assert not marker.exists()


def test_reaper_status_survives_a_non_utf8_index_path(tmp_path):
    from infra.worktree_reaper_probe import status_skip_reason, worktree_git_env

    repo = tmp_path / 'workspace'
    init(repo)
    git(repo, 'commit', '--allow-empty', '-qm', 'root')
    blob = subprocess.run(
        ['git', 'hash-object', '-w', '--stdin'], cwd=repo, input=b'one',
        capture_output=True, check=True,
    ).stdout.decode().strip()
    subprocess.run(
        ['git', 'update-index', '--index-info'], cwd=repo,
        input=f'100644 {blob} 0\t'.encode() + b'\xffname\n', check=True,
    )
    assert status_skip_reason(repo, worktree_git_env(repo, repo / '.git', repo)) == 'dirty'


def worktree_with_rewritten_commondir(tmp_path):
    """Return a repository and worktree whose admin commondir names agent-written config."""
    repo = tmp_path / 'workspace'
    init(repo)
    (repo / '.gitattributes').write_text('tracked filter=demo\n')
    (repo / 'tracked').write_text('one')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'root')
    worktree = tmp_path / 'worktree'
    git(repo, 'worktree', 'add', '-q', str(worktree))
    admin = Path(git(worktree, 'rev-parse', '--absolute-git-dir').stdout.strip())
    common = tmp_path / 'agent-common'
    shutil.copytree(repo / '.git', common)
    marker = tmp_path / 'ran'
    monitor, clean = tmp_path / 'monitor', tmp_path / 'clean'
    monitor.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\n')
    clean.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\ncat\n')
    monitor.chmod(0o755)
    clean.chmod(0o755)
    git(common.parent, 'config', '--file', str(common / 'config'), 'core.fsmonitor', str(monitor))
    git(common.parent, 'config', '--file', str(common / 'config'), 'filter.demo.clean', str(clean))
    (admin / 'commondir').write_text(f'{common}\n')
    git(worktree, 'status', '--porcelain')
    assert marker.exists(), 'control did not exercise the rewritten commondir'
    marker.unlink()
    os.utime(worktree / 'tracked', (1, 1))
    return repo, worktree, admin, marker


def test_reaper_reads_ignore_a_rewritten_commondir(tmp_path):
    from infra.worktree_reaper_probe import status_skip_reason, worktree_git_env
    from infra.worktree_reaper_salvage import salvage_worktree

    repo, worktree, admin, marker = worktree_with_rewritten_commondir(tmp_path)
    assert status_skip_reason(worktree, worktree_git_env(repo, admin, worktree)) is None
    (worktree / 'tracked').write_text('two')
    os.utime(worktree / 'tracked', (2, 2))
    (worktree / 'notes').write_text('keep')
    copied = salvage_worktree(repo, worktree, admin, 'worktree', 0, worktree)
    assert (copied / 'files' / 'notes').read_text() == 'keep'
    assert (copied / 'changes.patch').exists()
    assert not marker.exists()


def test_salvage_pin_lands_in_the_repository_or_fails(tmp_path):
    from infra.worktree_reaper_salvage import SalvageError, salvage_worktree

    repo, worktree, admin, _marker = worktree_with_rewritten_commondir(tmp_path)
    common = tmp_path / 'agent-common'
    copied = salvage_worktree(repo, worktree, admin, 'pinned', 0, worktree)
    meta = (copied / 'meta.txt').read_text().splitlines()
    pin = next(line for line in meta if line.startswith('pinned: ')).removeprefix('pinned: ')
    git(repo, 'rev-parse', '--verify', pin)
    assert not (common / pin).exists()

    tree = git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    foreign = git(tmp_path, f'--git-dir={common}', 'commit-tree', tree, '-m', 'elsewhere').stdout.strip()
    (common / 'refs' / 'heads' / 'worktree').write_text(f'{foreign}\n')
    with pytest.raises(SalvageError):
        salvage_worktree(repo, worktree, admin, 'foreign', 0, worktree)
    assert not (copied.parent / 'foreign').exists()


def test_reaper_removal_child_status_ignores_a_rewritten_commondir(tmp_path):
    from infra.worktree_reaper_remove import remove_worktree

    repo, worktree, _admin, marker = worktree_with_rewritten_commondir(tmp_path)
    assert remove_worktree(repo, worktree, 'worktree') == 'removed'
    assert not marker.exists()


def test_workspace_git_rules_and_no_repository(tmp_path):
    from tools.host_path_policy import ProtectedHostPathPolicy

    repo = tmp_path / 'workspace'
    init(repo)
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=repo)
    empty = tmp_path / 'empty'
    empty.mkdir()
    absent = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=empty)
    for workspace, rules in ((repo, policy.hard_floor_sandbox_rules()), (empty, absent.hard_floor_sandbox_rules())):
        patterns = re.findall(r'\(regex #"([^"]*\[\.\]\[Gg\]\[Ii\]\[Tt\][^"]*)"\)', rules)
        git_dir = workspace.resolve() / '.git'
        for target in (git_dir, git_dir / 'config', git_dir / 'config.lock', git_dir / 'config.worktree',
                       git_dir / 'commondir', git_dir / 'hooks', git_dir / 'hooks' / 'pre-commit'):
            for name in (str(target), str(target).upper()):
                assert any(re.search(pattern, name) for pattern in patterns), name
        for name in (git_dir / 'index', git_dir.parent / 'xgit', git_dir / 'hooks.old'):
            assert not any(re.search(pattern, str(name)) for pattern in patterns), name


def test_workspace_git_rules_keep_odd_paths_inside_the_profile():
    rules = workspace_git_rules((Path('/w "a[b]\\/.git'), Path('/w "a[b]\\/.git/hooks')))
    assert all(rule.count('"') == 2 and '\\' not in rule and '[b]' not in rule for rule in rules)


def test_file_tools_cannot_write_workspace_git_controls(tmp_path, monkeypatch):
    from tools import host_path_policy
    from tools.host_path_policy import ProtectedHostPathPolicy

    repo = tmp_path / 'workspace'
    init(repo)
    git = repo / '.git'
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=repo)
    monkeypatch.setattr(host_path_policy, 'current_protected_host_path_policy', lambda: policy)
    (repo / 'link').symlink_to(git / 'config')
    paths = (git / 'config', git / 'config.lock', git / 'commondir', git / 'hooks' / 'pre-commit', repo / 'link',
             repo / '.GIT' / 'CONFIG', repo / '.git' / 'Hooks' / 'pre-commit')
    for path in paths:
        assert 'No host-access lease' in policy.ordinary_denial(path, for_write=True)
        assert 'No host-access lease' in host_path_policy.path_review_classification(path, for_write=True)[1]
        assert policy.ordinary_denial(path, for_write=False) is None
    assert policy.ordinary_denial(git / 'index', for_write=True) is None
    assert policy.ordinary_denial(repo / 'notes.md', for_write=True) is None


def test_workspace_git_controls_in_all_macos_profiles(tmp_path, monkeypatch):
    from infra import config
    from tools import host_access_external, safehouse_launch, shell_guard

    workspace = tmp_path / 'workspace'
    init(workspace)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(shell_guard, '_safehouse_executable', lambda: tmp_path / 'safehouse')
    profile = tmp_path / 'profile.sb'
    profiles = [
        shell_guard.execution_envelope_profile('true', workspace, profile, leased=False, thread_id='test'),
        shell_guard.execution_envelope_profile('true', workspace, profile, leased=True, thread_id='test'),
        host_access_external.external_agent_sandbox_rules(profile, []),
        safehouse_launch.mcp_private_state_profile(profile, tmp_path / 'safehouse', tmp_path / 'state', tmp_path / 'cache'),
    ]
    rules = workspace_git_rules(workspace_git_paths(workspace))
    assert len(rules) == 6
    for rendered in profiles:
        assert all(rule in rendered for rule in rules)


def test_broken_head_keeps_workspace_git_rules(tmp_path):
    from tools.host_path_policy import ProtectedHostPathPolicy

    repo = tmp_path / 'workspace'
    init(repo)
    (repo / '.git' / 'HEAD').write_text('broken')
    rules = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=repo).hard_floor_sandbox_rules()
    assert all(rule in rules for rule in workspace_git_rules(workspace_git_paths(repo)))


def test_separate_git_dir_rules(tmp_path):
    repo = tmp_path / 'workspace'
    repo.mkdir()
    git(repo, 'init', '-q', '--separate-git-dir', str(repo / 'metadata'))
    paths = workspace_git_paths(repo)
    assert repo / '.git' in paths and repo / 'metadata' / 'config' in paths and repo / 'metadata' / 'commondir' in paths


def linux_launch(tmp_path, monkeypatch, workspace):
    """Wire the Linux shell path to ``workspace``; return (launch, the mounts it asked for)."""
    from infra import config, project_repo_policy
    from tools import linux_shell
    from tools.host_path_policy import ProtectedHostPathPolicy

    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=workspace)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(linux_shell, 'current_protected_host_path_policy', lambda: policy)
    monkeypatch.setattr(linux_shell, 'get_sandbox_add_dirs_rw', lambda *a, **k: [workspace])
    monkeypatch.setattr(linux_shell, 'get_allowed_dirs_ro', lambda **k: [])
    monkeypatch.setattr(linux_shell, 'agent_tool_ro_dirs', lambda: [])
    monkeypatch.setattr(project_repo_policy, 'list_unsupported_pinned_repos', lambda _p: [])
    monkeypatch.setattr(linux_shell.shutil, 'which', lambda _: '/usr/bin/bwrap')
    captured: dict = {}
    monkeypatch.setattr(linux_shell, 'build_protected_argv', lambda *a, **k: captured.update(k) or [])

    def launch(command: str = 'true') -> dict:
        linux_shell.shell_argv(command, workspace, thread_id='test', policies=[], env={})
        return captured
    return launch, captured


def test_linux_workspace_git_render(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    assert workspace / '.git' in captured['rw_roots']
    assert workspace / '.git/config' in captured['readonly_overlays']
    assert workspace / '.git/hooks' in captured['readonly_overlays']
    assert workspace / '.git/config.lock' not in captured['readonly_overlays']


def test_linux_launch_inits_an_absent_workspace_repository(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    git_dir = workspace / '.git'
    assert git_dir.is_dir() and (git_dir / 'HEAD').is_file()
    assert git_dir in captured['rw_roots']
    for name in ('config', 'commondir', 'config.worktree', 'hooks'):
        assert git_dir / name in captured['readonly_overlays'], name
    assert git_dir / 'config.lock' not in captured['readonly_overlays']
    assert git(workspace, 'status', '--porcelain').stdout == ''


def test_linux_launch_runs_no_git_when_the_repository_exists(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    launch, _captured = linux_launch(tmp_path, monkeypatch, workspace)

    def never(*_args, **_kwargs):
        raise AssertionError('a shell launch must not run Git when the workspace already has a repository')

    monkeypatch.setattr('infra.guidance_git.workspace_repository', never)
    launch()


def test_linux_launch_leaves_an_empty_marker_when_init_is_blocked(tmp_path, monkeypatch):
    from infra.guidance_git import workspace_repository
    from tools.linux_sandbox import protected_mounts

    outer = tmp_path / 'outer'
    workspace = outer / 'workspace'
    workspace.mkdir(parents=True)
    git(outer, 'init', '-q')
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    marker = workspace / '.git'
    assert marker.is_dir() and not any(marker.iterdir())
    assert workspace_repository(workspace) == (None, 'the workspace Git directory resolves elsewhere')
    controls = [path for path in workspace_git_paths(workspace)[1:] if path.suffix != '.lock']
    assert all(path not in captured['rw_roots'] for path in controls)
    grants = protected_mounts([workspace], [], controls)
    assert grants[marker] is True and grants[workspace] is False


def test_the_empty_workspace_marker_is_bound_read_only_in_the_rendered_argv(linux_sandbox_root, monkeypatch):
    """The real renderer must freeze just the empty marker, leaving the workspace writable."""
    from infra import config, project_repo_policy
    from tools import linux_shell
    from tools.host_path_policy import GENERATED_WORKSPACE_PROJECTIONS, ProtectedHostPathPolicy

    outer = linux_sandbox_root / 'outer'
    workspace = outer / 'workspace'
    workspace.mkdir(parents=True)
    # A real workspace has the generated projections; without them an unrelated absent path
    # would freeze the workspace root and hide what this test is about.
    for projection in GENERATED_WORKSPACE_PROJECTIONS:
        (workspace / projection).mkdir(parents=True)
    git(outer, 'init', '-q')
    home = linux_sandbox_root / 'home'
    home.mkdir(parents=True, exist_ok=True)
    policy = ProtectedHostPathPolicy(home=home, state_root=home / '.local/state/corsia', workspace=workspace)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(linux_shell, 'current_protected_host_path_policy', lambda: policy)
    monkeypatch.setattr(linux_shell, 'get_sandbox_add_dirs_rw', lambda *a, **k: [workspace])
    monkeypatch.setattr(linux_shell, 'get_allowed_dirs_ro', lambda **k: [])
    monkeypatch.setattr(linux_shell, 'agent_tool_ro_dirs', lambda: [])
    monkeypatch.setattr(project_repo_policy, 'list_unsupported_pinned_repos', lambda _p: [])
    monkeypatch.setattr(linux_shell.shutil, 'which', lambda _: '/usr/bin/bwrap')
    argv = linux_shell.shell_argv('true', workspace, thread_id='test', policies=[], env={})
    marker = str(workspace / '.git')

    def bound(path):
        return [argv[index] for index in range(len(argv) - 2)
                if argv[index] in ('--ro-bind', '--bind') and argv[index + 1] == path and argv[index + 2] == path]

    assert bound(marker) == ['--ro-bind']
    assert bound(str(workspace)) == ['--bind']


def test_an_empty_marker_is_inited_when_the_enclosing_repository_is_removed(tmp_path):
    from infra.guidance_git import workspace_repository

    outer = tmp_path / 'outer'
    workspace = outer / 'workspace'
    (workspace / '.git').mkdir(parents=True)
    git(outer, 'init', '-q')
    assert workspace_repository(workspace, create=True) == (None, 'the workspace is inside an enclosing repository')
    assert workspace_repository(workspace) is not None and workspace_repository(workspace)[0] is None
    shutil.rmtree(outer / '.git')
    assert workspace_repository(workspace, create=True) == (workspace.resolve(), None)
    assert (workspace / '.git' / 'HEAD').is_file()


def test_absent_commondir_and_config_worktree_are_created_and_overlaid(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    git_dir = workspace / '.git'
    assert (git_dir / 'commondir').read_text() == '.\n'
    assert (git_dir / 'config.worktree').read_bytes() == b''
    assert git_dir / 'commondir' in captured['readonly_overlays']
    assert git_dir / 'config.worktree' in captured['readonly_overlays']


def test_a_linked_worktree_keeps_its_commondir_and_gains_config_worktree(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    linked = tmp_path / 'linked'
    git(workspace, 'worktree', 'add', str(linked), '-b', 'linked')
    git_dir = Path(git(linked, 'rev-parse', '--absolute-git-dir').stdout.strip())
    launch, captured = linux_launch(tmp_path, monkeypatch, linked)
    launch()
    assert (git_dir / 'commondir').read_text() == '../..\n'
    assert (git_dir / 'config.worktree').read_bytes() == b''
    assert git_dir / 'commondir' in captured['readonly_overlays']
    assert git_dir / 'config.worktree' in captured['readonly_overlays']


def test_existing_git_control_files_are_never_overwritten(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    commondir = workspace / '.git' / 'commondir'
    worktree_config = workspace / '.git' / 'config.worktree'
    commondir.write_text('keep-me\n')
    worktree_config.write_text('keep-too\n')
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    assert commondir.read_text() == 'keep-me\n' and worktree_config.read_text() == 'keep-too\n'
    assert commondir in captured['readonly_overlays'] and worktree_config in captured['readonly_overlays']


def test_a_failed_placeholder_write_refuses_the_launch(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    real_open = os.open

    def blocked(path, flags, *args, **kwargs):
        if str(path).endswith('commondir'):
            raise OSError(30, 'Read-only file system')
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', blocked)
    with pytest.raises(RuntimeError, match='commondir') as refused:
        launch()
    assert 'free disk space' in str(refused.value) and 'Read-only file system' not in str(refused.value)
    assert captured == {}


def test_a_symlinked_placeholder_refuses_the_launch(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    init(workspace)
    (workspace / '.git' / 'commondir').symlink_to(tmp_path / 'planted-common')
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    with pytest.raises(RuntimeError, match='commondir') as refused:
        launch()
    assert captured == {}
    assert (workspace / '.git' / 'commondir').is_symlink()
    assert str(refused.value).startswith('Couldn’t protect the workspace Git path')


@pytest.mark.parametrize('dangling', [False, True])
def test_a_symlinked_marker_refuses_the_launch_before_any_git(tmp_path, monkeypatch, dangling):
    from infra import guidance_git

    target = tmp_path / 'target'
    init(target)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / '.git').symlink_to(tmp_path / 'gone' if dangling else target / '.git')
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    monkeypatch.setattr(guidance_git, '_git_bytes', lambda *a, **k: pytest.fail('Git ran on a refused launch'))
    with pytest.raises(RuntimeError, match='gitdir: file') as refused:
        launch()
    assert captured == {}
    assert (workspace / '.git').is_symlink() and not (target / '.git' / 'commondir').exists()
    assert str(refused.value).startswith('Couldn’t protect the workspace Git path')


def test_a_marker_that_points_nowhere_writes_no_placeholders(tmp_path, monkeypatch):
    from tools.linux_sandbox import protected_mounts

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / '.git').write_text('not a gitdir pointer\n')
    launch, captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    assert not (workspace / 'commondir').exists() and not (workspace / 'config.worktree').exists()
    assert workspace / '.git' in captured['readonly_overlays']
    controls = [path for path in workspace_git_paths(workspace)[1:] if path.suffix != '.lock']
    assert protected_mounts([workspace], [], controls)[workspace] is True


def plant_bare_layout(workspace, proof):
    """Write at the workspace root what makes Git take it for a bare repository, with a filter that runs on add."""
    (workspace / 'objects').mkdir()
    (workspace / 'refs').mkdir()
    (workspace / 'HEAD').write_text('ref: refs/heads/main\n')
    (workspace / 'config').write_text(
        '[core]\n\trepositoryformatversion = 0\n\tworktree = .\n'
        f'[filter "planted"]\n\tclean = "touch {proof}; cat"\n'
    )
    (workspace / '.gitattributes').write_text('*.txt filter=planted\n')
    (workspace / 'a.txt').write_text('x\n')


@pytest.mark.parametrize('state', ['deleted HEAD', 'empty marker'])
def test_a_bare_layout_at_the_workspace_root_is_never_adopted(tmp_path, state):
    from infra.guidance_git import GitFault, _git, workspace_repository

    workspace = tmp_path / 'workspace'
    proof = tmp_path / 'filter-ran'
    if state == 'deleted HEAD':
        init(workspace)
        (workspace / '.git' / 'HEAD').unlink()
    else:
        (workspace / '.git').mkdir(parents=True)
    plant_bare_layout(workspace, proof)
    assert subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=workspace).returncode == 0
    repo, reason = workspace_repository(workspace)
    assert repo is None and 'bare repository' in reason
    with pytest.raises(GitFault, match='bare repository'):
        _git(workspace, 'add', 'a.txt')
    assert not proof.exists()


def test_a_save_makes_a_real_repository_beside_a_bare_layout(tmp_path):
    from infra.guidance_git import workspace_repository

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    plant_bare_layout(workspace, tmp_path / 'filter-ran')
    assert workspace_repository(workspace, create=True) == (workspace.resolve(), None)
    assert (workspace / '.git' / 'HEAD').is_file()
    assert workspace_repository(workspace) == (workspace.resolve(), None)


def test_a_separate_git_dir_gets_the_placeholders_in_its_metadata_dir(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    metadata = tmp_path / 'metadata'
    git(workspace, 'init', '-q', '--separate-git-dir', str(metadata))
    launch, _captured = linux_launch(tmp_path, monkeypatch, workspace)
    launch()
    assert (metadata / 'commondir').read_text() == '.\n'
    assert (metadata / 'config.worktree').read_bytes() == b''
    assert git(workspace, 'status', '--porcelain').stdout == ''


def test_dot_commondir_keeps_the_repository_and_its_worktrees_working(tmp_path):
    """The backend's ``.`` placeholder must not change how Git reads the workspace repository."""
    from tools.linux_workspace_git import prepare_workspace_git

    workspace = tmp_path / 'workspace'
    init(workspace)
    prepare_workspace_git(workspace)
    assert (workspace / '.git' / 'commondir').read_text() == '.\n'
    assert git(workspace, 'rev-parse', '--show-toplevel').stdout.strip() == str(workspace.resolve())
    assert git(workspace, 'rev-parse', '--git-common-dir').stdout.strip() == str(workspace / '.git')
    assert git(workspace, 'status', '--porcelain').stdout == ''
    assert git(workspace, 'commit', '--allow-empty', '-qm', 'x').returncode == 0
    linked = tmp_path / 'linked'
    git(workspace, 'worktree', 'add', str(linked), '-b', 'linked')
    listed = [line.split()[0] for line in git(workspace, 'worktree', 'list').stdout.splitlines()]
    assert listed == [str(workspace.resolve()), str(linked.resolve())]
    assert git(linked, 'rev-parse', '--git-common-dir').stdout.strip() == str(workspace / '.git')
    assert git(workspace, 'worktree', 'remove', str(linked)).returncode == 0


def safehouse_runner(workspace, tmp_path, monkeypatch):
    from infra import config
    from tools import sandbox_config, shell_guard

    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [workspace])
    profile = tmp_path / 'profile.sb'
    profile.write_text(shell_guard.execution_envelope_profile('true', workspace, profile, leased=False, thread_id='workspace-test'))

    def run(command):
        return subprocess.run(shell_guard.safehouse_argv(command, workspace, profile, thread_id='workspace-test'),
                              cwd=workspace, env=shell_guard.safehouse_subprocess_env('workspace-test'),
                              capture_output=True, text=True, timeout=30)
    return run


def require_safehouse():
    from tests.test_shell_execution_envelopes import _safehouse_integration_unavailable

    if os.environ.get('CORSIA_THREAD_ID'):
        _safehouse_integration_unavailable('agent shells cannot prove nested Safehouse confinement')
    if not shutil.which('safehouse'):
        _safehouse_integration_unavailable('Agent Safehouse is unavailable')


@pytest.mark.integration
def test_safehouse_refuses_an_agent_made_workspace_repository(tmp_path, monkeypatch):
    require_safehouse()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    run = safehouse_runner(workspace, tmp_path, monkeypatch)
    for command in ('mkdir .git', 'mkdir .GIT', 'git init -q', 'printf "gitdir: /tmp" > .git', 'ln -s /tmp .git'):
        assert run(command).returncode != 0, command
        assert not (workspace / '.git').exists(), command
    assert run('printf x > file').returncode == 0


@pytest.mark.integration
@pytest.mark.parametrize('separate', [False, True])
def test_safehouse_workspace_git_controls(tmp_path, monkeypatch, separate):
    require_safehouse()
    workspace = tmp_path / 'workspace'
    if separate:
        workspace.mkdir()
        git(workspace, 'init', '-q', '--separate-git-dir', str(workspace / 'metadata'))
        git(workspace, 'config', 'user.name', 'Test')
        git(workspace, 'config', 'user.email', 'test@example.invalid')
    else:
        init(workspace)
    run = safehouse_runner(workspace, tmp_path, monkeypatch)
    git_dir = 'metadata' if separate else '.git'
    for target in (f'{git_dir.upper()}/CONFIG', f'{git_dir}/COMMONDIR', f'{git_dir}/CONFIG.WORKTREE', f'{git_dir}/config', f'{git_dir}/config.lock', f'{git_dir}/config.worktree', f'{git_dir}/commondir', f'{git_dir}/hooks/test'):
        result = run(f'printf x > {shlex.quote(target)}')
        assert result.returncode != 0, target
    linked = run(f'ln {shlex.quote(git_dir + "/config")} alias')
    if linked.returncode == 0:
        written = run('printf x > alias')
        pytest.fail(f'Git config hard link was created; write exit status: {written.returncode}')
    result = run('mv .git saved-git')
    assert result.returncode != 0
    assert (workspace / '.git').is_file() if separate else (workspace / '.git').is_dir()
    result = run('printf content > file && git add file && git commit -qm test')
    assert result.returncode == 0, result.stderr
    assert git(workspace, 'status', '--porcelain').stdout == ('?? metadata/\n' if separate else '')
