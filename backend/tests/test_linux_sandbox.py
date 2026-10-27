"""Mount planning must preserve overlays without creating host mountpoints."""
from pathlib import Path
import os
import pytest
from tools.host_path_policy import ProtectedHostPathPolicy
from tools.linux_sandbox import HOME_RW, build_protected_argv, protected_mounts, thread_tmp_root
from tests.linux_sandbox_test_support import linux_sandbox_root


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


def test_linux_socket_staging_uses_thread_tmp_owner(tmp_path, monkeypatch):
    from infra import config
    from tools import unix_socket_policy
    monkeypatch.setattr(config, 'STATE_ROOT', tmp_path / 'state')
    monkeypatch.setattr(unix_socket_policy.sys, 'platform', 'linux')
    root = unix_socket_policy.corsia_temp_root_for_bind('one')
    assert root == thread_tmp_root(config.STATE_ROOT, 'one')
    assert unix_socket_policy.thread_bind_roots('one') == []
    assert root.is_dir()


def test_cache_creation_refuses_symlink_ancestor(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (home / '.cache').symlink_to(outside, target_is_directory=True)
    policy = ProtectedHostPathPolicy(home, home / '.local/state/corsia')
    with pytest.raises(OSError):
        build_protected_argv('true', home, path_policy=policy, rw_roots=[], ro_roots=[],
            readonly_overlays=[], env={}, thread_id='one', executable='/usr/bin/bwrap')
    assert list(outside.iterdir()) == []


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
    for relative in HOME_RW:
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
