"""Mount planning must preserve overlays without creating host mountpoints."""
from pathlib import Path
import os
import pytest
from tools.host_path_policy import ProtectedHostPathPolicy
from tools.linux_sandbox import HOME_CACHES, HOME_RW, build_protected_argv, protected_mounts, thread_tmp_root
from tests.linux_sandbox_test_support import linux_sandbox_root


def test_agent_account_personal_gh_home_is_masked(tmp_path, monkeypatch):
    from connections import github_agent
    from infra import config, project_repo_policy
    from tools import linux_shell
    home = tmp_path / 'home'
    gh = home / '.config/gh'
    gh.mkdir(parents=True)
    (gh / 'hosts.yml').write_text('dummy-marker')
    workspace = home / 'work'
    workspace.mkdir()
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia')
    monkeypatch.setattr(linux_shell, 'current_protected_host_path_policy', lambda: policy)
    monkeypatch.setattr(linux_shell, 'get_sandbox_add_dirs_rw', lambda *a, **k: [home, gh, workspace])
    monkeypatch.setattr(linux_shell, 'get_allowed_dirs_ro', lambda **k: [])
    monkeypatch.setattr(linux_shell, 'agent_tool_ro_dirs', lambda: [])
    monkeypatch.setattr(project_repo_policy, 'list_unsupported_pinned_repos', lambda _: [])
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setattr(github_agent, 'default_home', lambda: gh)
    monkeypatch.setattr(linux_shell.shutil, 'which', lambda _: '/usr/bin/bwrap')
    account = github_agent.AgentAccountView(state='needs_auth')
    argv = linux_shell.shell_argv('true', workspace, thread_id='one', policies=[], env={}, agent_account=account)
    assert any(argv[i:i + 4] == ['--perms', '0000', '--tmpfs', str(gh)] for i in range(len(argv)))
    assert not any(argv[i:i + 3] == ['--ro-bind', str(gh), str(gh)] for i in range(len(argv)))


def test_protected_overlay_pins_every_ancestor_before_overlay(linux_sandbox_root):
    home = linux_sandbox_root / 'home'
    workspace = home / 'work'
    protected = workspace / 'repo/.git/logs/HEAD'
    protected.parent.mkdir(parents=True)
    protected.write_text('intact')
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia')
    argv = build_protected_argv('true', workspace, path_policy=policy,
        rw_roots=[workspace], ro_roots=[], readonly_overlays=[protected],
        env={}, thread_id='one', executable='/usr/bin/bwrap')
    from tools.linux_keyring_launcher import launcher_argv
    assert argv[:4] == launcher_argv(['/usr/bin/bwrap'])
    cursor = 0
    for path in (workspace / 'repo', workspace / 'repo/.git', protected.parent):
        index = argv.index(str(path), cursor)
        assert argv[index - 1] == '--bind'
        assert argv[index + 1] == str(path)
        cursor = index + 2
    assert argv[argv.index(str(protected), cursor) - 1] == '--ro-bind'
    assert '--unshare-user' in argv and '--disable-userns' in argv


def test_missing_protected_path_freezes_parent_without_creating_it(tmp_path):
    common = tmp_path / '.git'
    objects = common / 'objects'
    objects.mkdir(parents=True)
    protected = common / 'packed-refs'
    mounts = protected_mounts([tmp_path, objects], [], [protected])
    assert mounts[common] is True
    assert mounts[objects] is False
    assert protected not in mounts
    assert not protected.exists()
    assert sorted(common.iterdir()) == [objects]


def test_pin_does_not_reopen_protected_base_or_hide_rw_child(tmp_path):
    repo = tmp_path / 'repo'
    worktree = repo / '.worktrees/one'
    worktree.mkdir(parents=True)
    marker = worktree / '.git'
    marker.write_text('fixture')
    mounts = protected_mounts([tmp_path, worktree], [repo], [marker])
    assert mounts[repo] is True
    assert mounts[repo / '.worktrees'] is True
    assert mounts[worktree] is False


@pytest.mark.parametrize('readonly', [False, True])
def test_root_filesystem_grant_is_refused(tmp_path, readonly):
    policy = ProtectedHostPathPolicy(tmp_path, tmp_path / 'state')
    with pytest.raises(ValueError, match='specific directories'):
        build_protected_argv('true', tmp_path, path_policy=policy,
            rw_roots=[] if readonly else [Path('/')],
            ro_roots=[Path('/')] if readonly else [], readonly_overlays=[],
            env={}, thread_id='one', executable='/usr/bin/bwrap')
    assert not (tmp_path / 'state').exists()


def test_protected_runtime_cache_created_and_readonly(tmp_path, monkeypatch):
    from tools import managed_runtime
    monkeypatch.setattr(managed_runtime.platform, 'system', lambda: 'Linux')
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    home = tmp_path / 'home'
    home.mkdir()
    cache = home / '.cache/corsia'
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia', write_roots=(cache,))
    argv = build_protected_argv('true', home, path_policy=policy, rw_roots=[home], ro_roots=[],
        readonly_overlays=[], env={}, thread_id='one', executable='/usr/bin/bwrap')
    assert cache.stat().st_mode & 0o777 == 0o700
    assert cache.stat().st_uid == os.getuid()
    assert any(argv[i:i + 3] == ['--ro-bind', str(cache), str(cache)] for i in range(len(argv)))


def test_symlinked_protected_runtime_cache_refuses_command(tmp_path, monkeypatch):
    from tools import managed_runtime
    monkeypatch.setattr(managed_runtime.platform, 'system', lambda: 'Linux')
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    home = tmp_path / 'home'
    (home / '.cache').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    cache = home / '.cache/corsia'
    cache.symlink_to(outside, target_is_directory=True)
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia', write_roots=(cache,))
    with pytest.raises(ValueError, match="Couldn't protect CORSIA's cache folder"):
        build_protected_argv('true', home, path_policy=policy, rw_roots=[home], ro_roots=[],
            readonly_overlays=[], env={}, thread_id='one', executable='/usr/bin/bwrap')
    assert not list(outside.iterdir())


def test_cache_creation_skips_symlink_ancestor(tmp_path, caplog):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / '.cache').mkdir()
    (home / '.cache/uv').symlink_to(outside, target_is_directory=True)
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia')
    argv = build_protected_argv('true', home, path_policy=policy, rw_roots=[], ro_roots=[],
        readonly_overlays=[], env={}, thread_id='one', executable='/usr/bin/bwrap')
    assert 'Skipping unavailable cache path' in caplog.text
    assert str(outside) not in argv
    assert list(outside.iterdir()) == []


def test_cache_creation_skips_unwritable_optional_cache(tmp_path, caplog, monkeypatch):
    from tools import managed_runtime
    monkeypatch.setattr(managed_runtime.platform, 'system', lambda: 'Linux')
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    home = tmp_path / 'home'
    # The protected CORSIA cache already exists; only the optional ones can't be created.
    (home / '.cache/corsia').mkdir(parents=True, mode=0o700)
    (home / '.cache').chmod(0o500)
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia')
    try:
        build_protected_argv('true', home, path_policy=policy, rw_roots=[], ro_roots=[],
            readonly_overlays=[], env={}, thread_id='one', executable='/usr/bin/bwrap')
    finally:
        (home / '.cache').chmod(0o700)
    assert 'Skipping unavailable cache path' in caplog.text


def test_home_grant_does_not_create_missing_persistence_paths(linux_sandbox_root):
    home = linux_sandbox_root / 'home'
    home.mkdir()
    state = home / '.local/state/corsia'
    policy = ProtectedHostPathPolicy(home, state)
    argv = build_protected_argv('true', home, path_policy=policy,
        rw_roots=[home, Path('/run')], ro_roots=[], readonly_overlays=[],
        env={}, thread_id='one', executable='/usr/bin/bwrap')
    mounts = protected_mounts([home], [], [home / '.bashrc'])
    assert mounts[home] is True
    assert not (home / '.bashrc').exists()
    assert not (home / '.config').exists()
    assert not (home / '.ssh').exists()
    assert str(home / '.bashrc') not in argv
    for relative in set(HOME_RW) - set(HOME_CACHES):
        assert not (home / relative).exists()
    for relative in HOME_CACHES:
        cache = home / relative
        assert cache.is_dir()
        assert cache.stat().st_mode & 0o777 == 0o700
        assert cache.stat().st_uid == os.getuid()
        assert ['--bind', str(cache), str(cache)] == argv[argv.index(str(cache)) - 1:argv.index(str(cache)) + 2]
    run_mount = ['--perms', '0111', '--tmpfs', '/run']
    position = next(i for i in range(len(argv)) if argv[i:i + 4] == run_mount)
    assert position > argv.index('--bind')
    assert argv[position + 4:position + 8] == ['--perms', '0111', '--tmpfs', '/run/systemd']
    assert thread_tmp_root(state, 'one') != thread_tmp_root(state, 'two')
    assert thread_tmp_root(state, 'one').is_relative_to(state)
