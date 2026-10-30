from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


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
    from infra.worktree_reaper_probe import status_skip_reason
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
    assert status_skip_reason(repo) == 'submodule'
    assert _status_verdict(None, None, repo, 'submodule', 0, 0) == 'submodule'
    with pytest.raises(SalvageError, match='submodule'):
        _selected(repo)
    assert not marker.exists()


def test_workspace_git_rules_and_no_repository(tmp_path):
    from tools.host_path_policy import ProtectedHostPathPolicy

    repo = tmp_path / 'workspace'
    init(repo)
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=repo)
    rules = policy.hard_floor_sandbox_rules()
    for name in ('config', 'config.lock', 'config.worktree', 'hooks'):
        assert str(repo / '.git' / name) in rules
    assert f'(literal "{repo / ".git"}")' in rules
    assert str(repo / '.git' / 'index') not in rules
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert not ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=empty).workspace_git_paths()


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
    for rendered in profiles:
        for path in (workspace / '.git', workspace / '.git/config', workspace / '.git/config.lock',
                     workspace / '.git/config.worktree', workspace / '.git/hooks'):
            assert str(path) in rendered


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


@pytest.mark.integration
@pytest.mark.parametrize('separate', [False, True])
def test_safehouse_workspace_git_controls(tmp_path, monkeypatch, separate):
    from infra import config
    from tools import sandbox_config, shell_guard
    from tests.test_shell_execution_envelopes import _safehouse_integration_unavailable

    if os.environ.get('CORSIA_THREAD_ID'):
        _safehouse_integration_unavailable('agent shells cannot prove nested Safehouse confinement')
    if not shutil.which('safehouse'):
        _safehouse_integration_unavailable('Agent Safehouse is unavailable')
    workspace = tmp_path / 'workspace'
    if separate:
        workspace.mkdir()
        git(workspace, 'init', '-q', '--separate-git-dir', str(workspace / 'metadata'))
        git(workspace, 'config', 'user.name', 'Test')
        git(workspace, 'config', 'user.email', 'test@example.invalid')
    else:
        init(workspace)
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [workspace])
    profile = tmp_path / 'profile.sb'
    profile.write_text(shell_guard.execution_envelope_profile('true', workspace, profile, leased=False, thread_id='workspace-test'))

    def run(command):
        return subprocess.run(shell_guard.safehouse_argv(command, workspace, profile, thread_id='workspace-test'),
                              cwd=workspace, env=shell_guard.safehouse_subprocess_env('workspace-test'),
                              capture_output=True, text=True, timeout=30)

    git_dir = 'metadata' if separate else '.git'
    for target in (f'{git_dir}/config', f'{git_dir}/config.lock', f'{git_dir}/config.worktree', f'{git_dir}/hooks/test'):
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
    assert git(workspace, 'status', '--porcelain').stdout == ''
