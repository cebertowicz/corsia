from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest

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


def test_linux_workspace_git_render(tmp_path, monkeypatch):
    from infra import config
    from tools import linux_shell
    from tools.host_path_policy import ProtectedHostPathPolicy

    workspace = tmp_path / 'workspace'
    init(workspace)
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=workspace)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(linux_shell, 'current_protected_host_path_policy', lambda: policy)
    monkeypatch.setattr(linux_shell, 'get_sandbox_add_dirs_rw', lambda *a, **k: [workspace])
    monkeypatch.setattr(linux_shell, 'get_allowed_dirs_ro', lambda **k: [])
    monkeypatch.setattr(linux_shell.shutil, 'which', lambda _: '/usr/bin/bwrap')
    captured = {}
    monkeypatch.setattr(linux_shell, 'build_protected_argv', lambda *a, **k: captured.update(k) or [])
    linux_shell.shell_argv('true', workspace, thread_id='test', policies=[], env={})
    assert workspace / '.git' in captured['rw_roots']
    assert workspace / '.git/config' in captured['readonly_overlays']
    assert workspace / '.git/hooks' in captured['readonly_overlays']
    assert workspace / '.git/config.lock' not in captured['readonly_overlays']


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
