import re

import pytest


@pytest.mark.parametrize('kind', ['directory', 'pointer', 'absent'])
def test_all_macos_profiles_deny_workspace_git_reads(tmp_path, monkeypatch, kind):
    from infra import config
    from tools import host_access_external, safehouse_launch, shell_guard

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    marker = workspace / '.git'
    target = marker
    if kind == 'directory':
        marker.mkdir()
    elif kind == 'pointer':
        target = tmp_path / 'actual-git'
        target.mkdir()
        marker.write_text(f'gitdir: {target}\n')
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
        patterns = re.findall(r'\(deny file-read\* file-write\* \(regex #"([^"]+)"\)\)', rendered)
        for name in (str(target / 'HEAD'), str(target / 'HEAD').upper(), str(marker)):
            assert any(re.search(pattern, name) for pattern in patterns), (kind, name)
        assert f'(deny file-read* file-write* (subpath "{target.resolve()}"))' in rendered
        assert f'(allow file-read* file-write* (subpath "{target.resolve()}"))' not in rendered


def test_file_policy_refuses_git_history_reads(tmp_path):
    from tools.host_path_policy import ProtectedHostPathPolicy

    workspace = tmp_path / 'workspace'
    (workspace / '.git').mkdir(parents=True)
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=workspace)
    assert policy.ordinary_denial(workspace / '.git' / 'HEAD', for_write=False)
    assert policy.ordinary_denial(workspace / 'notes.md', for_write=False) is None


@pytest.mark.asyncio
async def test_read_file_refuses_workspace_git_history(tmp_path, monkeypatch):
    from tools import host_path_policy
    from tools.filesystem_rw import ReadFileTool
    from tools.host_path_policy import ProtectedHostPathPolicy

    workspace = tmp_path / 'workspace'
    (workspace / '.git').mkdir(parents=True)
    (workspace / '.git' / 'HEAD').write_text('private history')
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state', workspace=workspace)
    monkeypatch.setattr(host_path_policy, 'current_protected_host_path_policy', lambda **kwargs: policy)
    result = await ReadFileTool(workspace=workspace).execute(path=str(workspace / '.git' / 'HEAD'), _thread_id='test')
    assert 'private history' not in str(result)
    assert 'blocked' in str(result)


def test_pointer_and_common_directory_lexical_and_resolved_paths(tmp_path):
    from tools.workspace_git_paths import workspace_git_history_paths

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    actual = tmp_path / 'actual'
    actual.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(actual, target_is_directory=True)
    (workspace / '.git').write_text('gitdir: ../alias\n')
    common = tmp_path / 'common'
    common.mkdir()
    (actual / 'commondir').write_text('../common\n')
    paths = workspace_git_history_paths(workspace)
    assert {workspace / '.git', alias, actual, common} <= set(paths)


def test_invalid_marker_does_not_deny_workspace(tmp_path):
    from tools.workspace_git_paths import workspace_git_history_paths

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / '.git').write_text('invalid')
    assert workspace not in workspace_git_history_paths(workspace)
