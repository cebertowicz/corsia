"""Real bubblewrap twins of shell and script enforcement, in Ubuntu's unit job."""
from __future__ import annotations
from dataclasses import replace
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.linux_sandbox_test_support import linux_sandbox_root
from tools.host_path_policy import ProtectedHostPathPolicy
from tools.linux_sandbox import HOME_CACHES, build_protected_argv, cleanup_thread_tmp, thread_tmp_root

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('bwrap'),
                                reason='Linux with bubblewrap required')


@pytest.fixture
def linux_box(linux_sandbox_root, monkeypatch, isolated_db):
    from tools import linux_shell, sandbox_config
    from infra import config
    home = linux_sandbox_root / 'home'
    workspace = home / 'work'
    repo = workspace / 'repo'
    for directory in (repo / '.git/logs', workspace / '.corsia/context',
                      workspace / '.corsia/skills', workspace / 'base',
                      home / 'Documents', home / '.ssh',
                      home / '.config/systemd/user', home / '.config/autostart'):
        directory.mkdir(parents=True)
    for path in (repo / '.git/HEAD', repo / '.git/logs/HEAD',
                 workspace / '.corsia/context/A.md', workspace / '.corsia/skills/A.md'):
        path.write_text('intact\n')
    (home / '.ssh/id_ed25519').write_text('fixture-private')
    (home / '.ssh/config').write_text('# fixture allowed config')
    (home / 'Documents/private').write_text('fixture-private')
    (home / '.bashrc').write_text('fixture-persistence')
    state = home / '.local/state/corsia'
    (state / 'keys').mkdir(parents=True)
    (state / 'keys/private').write_text('fixture-private')
    policy = ProtectedHostPathPolicy(home, state)
    overlays = [repo / '.git/HEAD', repo / '.git/logs/HEAD',
                workspace / '.corsia/context', workspace / '.corsia/skills']
    monkeypatch.setattr(linux_shell, 'current_protected_host_path_policy', lambda **_: policy)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [workspace])
    monkeypatch.setattr(config, 'WORKSPACE', workspace)
    monkeypatch.setenv('HOME', str(home))
    from infra.database import get_db
    db = get_db()
    try:
        for thread in ('one', 'two'):
            db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?, 'user', 't', 't')", (thread,))
        db.commit()
    finally:
        db.close()
    env = {'HOME': str(home), 'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}

    def argv_for(command, *, thread='one', home_grant=False, extra_roots=()):
        rw = [home, Path('/run')] if home_grant else [workspace]
        return build_protected_argv(command, workspace, path_policy=policy,
            rw_roots=[*rw, *extra_roots],
            ro_roots=[workspace / 'base'], readonly_overlays=overlays,
            env=env, thread_id=thread, executable=shutil.which('bwrap'))

    def run(command, **kwargs):
        return subprocess.run(argv_for(command, **kwargs), capture_output=True, text=True, timeout=20)
    return SimpleNamespace(home=home, workspace=workspace, repo=repo, state=state,
                           policy=policy, run=run, argv_for=argv_for, env=env)


def shell_workspace_git_runner(box, monkeypatch, fence=None):
    """Run commands through the real Linux shell path, so the backend prepares the workspace Git paths."""
    from connections import github_agent
    from tools import linux_shell
    from tools.project_fence import Fence

    policy = replace(box.policy, workspace=box.workspace, fence=fence or Fence())
    monkeypatch.setattr(linux_shell, 'current_protected_host_path_policy', lambda **_: policy)
    monkeypatch.setattr(github_agent, 'default_home', lambda: box.home / '.config/gh')
    monkeypatch.setattr(linux_shell, 'get_sandbox_add_dirs_rw', lambda *a, **k: [box.workspace])
    monkeypatch.setattr(linux_shell, 'get_allowed_dirs_ro', lambda **k: [])
    monkeypatch.setattr(linux_shell, 'agent_tool_ro_dirs', lambda: [])
    account = github_agent.AgentAccountView(state='needs_auth')

    def run(command):
        argv = linux_shell.shell_argv(command, box.workspace, thread_id='one', policies=[],
                                      env=box.env, agent_account=account)
        return subprocess.run(argv, capture_output=True, text=True, timeout=20)
    return run


def host_git(workspace, *args):
    return subprocess.run(['git', '-C', str(workspace), '-c', 'user.name=T', '-c', 'user.email=t@example.invalid',
                           *args], capture_output=True, text=True, timeout=20)


def test_the_workspace_git_history_is_hidden_from_a_real_shell(linux_box, monkeypatch):
    box = linux_box
    run = shell_workspace_git_runner(box, monkeypatch)
    assert not (box.workspace / '.git').exists()
    result = run('true')
    assert result.returncode == 0, result.stderr
    git_dir = box.workspace / '.git'
    assert git_dir.is_dir() and (git_dir / 'HEAD').is_file()
    # Content that now lives only in history, as another project's deleted file would.
    (box.workspace / 'gone.md').write_text('history-marker\n')
    assert host_git(box.workspace, 'add', 'gone.md').returncode == 0
    assert host_git(box.workspace, 'commit', '-qm', 'x').returncode == 0
    (box.workspace / 'gone.md').unlink()
    for command in ('git log -p', 'git --git-dir=.git log -p', 'git cat-file -p HEAD', 'cat .git/HEAD',
                    'ls .git', 'find .git -type f', 'grep -r history-marker .git',
                    'ln -snfT /tmp .git', 'touch .git/commondir', 'printf x > .git/config',
                    'printf x > .git/hooks/pre-commit', 'rm .git/HEAD', 'mv .git moved', 'rm -rf .git',
                    'git -c user.name=T -c user.email=t@example.invalid commit --allow-empty -m y'):
        result = run(command)
        print(f'{command}: exit={result.returncode} stderr={result.stderr.strip()}')
        assert result.returncode != 0, command
        assert 'history-marker' not in result.stdout, command
    assert (git_dir / 'HEAD').is_file() and 'x' not in (git_dir / 'config').read_text()
    assert not (git_dir / 'hooks' / 'pre-commit').exists() and not (git_dir / 'commondir').exists()
    assert host_git(box.workspace, 'rev-list', '--count', 'HEAD').stdout.strip() == '1'
    # Control: the rest of the workspace stays readable and writable.
    result = run('echo kept > sibling && cat sibling')
    assert result.returncode == 0 and result.stdout == 'kept\n', result.stderr


def test_a_bare_layout_planted_by_a_real_shell_is_not_adopted(linux_box, monkeypatch):
    from infra.guidance_git import workspace_repository

    box = linux_box
    run = shell_workspace_git_runner(box, monkeypatch)
    assert run('true').returncode == 0
    result = run("mkdir objects refs && echo 'ref: refs/heads/main' > HEAD "
                 "&& printf '[core]\\n\\trepositoryformatversion = 0\\n\\tworktree = .\\n' > config")
    print(f'plant: exit={result.returncode} stderr={result.stderr.strip()}')
    assert result.returncode == 0, result.stderr
    assert workspace_repository(box.workspace) == (box.workspace.resolve(), None)


def test_a_marker_that_points_nowhere_hides_only_itself_in_a_real_shell(linux_box, monkeypatch):
    box = linux_box
    (box.workspace / '.git').write_text('not a gitdir pointer\n')
    run = shell_workspace_git_runner(box, monkeypatch)
    for command in ('cat .git', 'printf "gitdir: /tmp" > .git', 'mv .git moved', 'rm -f .git'):
        result = run(command)
        print(f'{command}: exit={result.returncode} stderr={result.stderr.strip()}')
        assert result.returncode != 0, command
    assert (box.workspace / '.git').read_text() == 'not a gitdir pointer\n'
    # The workspace root is not frozen: new entries, control-file names included, can still be made.
    result = run('touch new-entry && mkdir hooks && touch config')
    assert result.returncode == 0, result.stderr


def test_an_enclosing_repository_leaves_an_unplanted_marker_in_a_real_shell(linux_box, monkeypatch):
    """With no repository of its own the workspace gets an empty marker no shell can open or fill in."""
    box = linux_box
    subprocess.run(['git', 'init', '--quiet'], cwd=str(box.home), check=True, timeout=30)
    run = shell_workspace_git_runner(box, monkeypatch)
    result = run('true')
    assert result.returncode == 0, result.stderr
    marker = box.workspace / '.git'
    assert marker.is_dir() and not any(marker.iterdir())
    for command in ('mkdir .git', 'touch .git/commondir', 'touch .git/config.worktree', 'printf x > .git/config',
                    'rmdir .git', 'mv .git moved'):
        result = run(command)
        print(f'{command}: exit={result.returncode} stderr={result.stderr.strip()}')
        assert result.returncode != 0, command
    assert not any(marker.iterdir())
    result = run('touch new-entry')
    assert result.returncode == 0, result.stderr
    assert (box.workspace / 'new-entry').exists()


def test_another_project_is_hidden_from_a_real_shell(linux_box, monkeypatch):
    from tools import project_fence
    from tools.host_path_resolution import linux_denied_paths_in
    from tools.project_fence import Fence, FencedRoot
    monkeypatch.setattr(project_fence, '_event', lambda *_a, **_k: None)
    box = linux_box
    projects = box.workspace / 'projects'
    for name in ('own', 'other'):
        (projects / name).mkdir(parents=True)
        (projects / name / 'notes.md').write_text(f'{name}-marker\n')
    (box.workspace / 'own-link').symlink_to(projects / 'other')
    fence = Fence((FencedRoot(projects / 'other', 'other'), FencedRoot(projects / 'later', 'later')), 'one')
    run = shell_workspace_git_runner(box, monkeypatch, fence)
    for command in ('ls projects/other/notes.md', 'cat projects/other/notes.md', 'cat own-link/notes.md',
                    'touch projects/other/new', 'mv projects/other projects/moved',
                    'mv projects projects-moved', 'rm -rf projects/other'):
        result = run(command)
        print(f'{command}: exit={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}')
        assert result.returncode != 0, command
        assert 'other-marker' not in result.stdout
        if not command.startswith(('mv', 'rm')):  # a mount point refuses those as busy, not denied
            named = [fence.refusal(path) for path in linux_denied_paths_in(result.stderr, box.workspace)]
            assert any(message and 'in project `other`' in message for message in named), result.stderr
    assert (projects / 'other/notes.md').read_text() == 'other-marker\n'
    assert not (projects / 'other/new').exists()
    # Control: the caller's own project stays readable and writable.
    result = run('cat projects/own/notes.md && touch projects/own/new')
    assert result.returncode == 0 and result.stdout == 'own-marker\n', result.stderr
    # An absent fenced root is skipped rather than freezing projects/; the next command masks it.
    result = run('mkdir projects/later && echo later-marker > projects/later/notes.md && touch projects/fresh')
    assert result.returncode == 0, result.stderr
    result = run('cat projects/later/notes.md')
    assert result.returncode != 0 and 'later-marker' not in result.stdout


def test_personal_gh_home_hidden_with_agent_account(linux_box, monkeypatch):
    from connections import github_agent
    from tools import linux_shell
    box = linux_box
    gh = box.home / '.config/gh'
    gh.mkdir(parents=True)
    marker = 'dummy-personal-oauth-marker'
    (gh / 'hosts.yml').write_text(marker)
    cache = box.home / '.cache/corsia'
    cache.mkdir(parents=True)
    (cache / 'readable').write_text('runtime-control')
    monkeypatch.setattr(github_agent, 'default_home', lambda: gh)
    monkeypatch.setattr(linux_shell, 'get_sandbox_add_dirs_rw', lambda *a, **k: [box.home, box.workspace, gh])
    monkeypatch.setattr(linux_shell, 'get_allowed_dirs_ro', lambda **k: [])
    monkeypatch.setattr(linux_shell, 'agent_tool_ro_dirs', lambda: [])
    account = github_agent.AgentAccountView(state='needs_auth')
    def run(command):
        argv = linux_shell.shell_argv(command, box.workspace, thread_id='one', policies=[],
                                      env=box.env, agent_account=account)
        return subprocess.run(argv, capture_output=True, text=True, timeout=20)
    for command in (f'cat {shlex.quote(str(gh / "hosts.yml"))}',
                    f'ls {shlex.quote(str(gh))}', f'touch {shlex.quote(str(gh / "new"))}'):
        result = run(command)
        print(f'personal gh denied: exit={result.returncode} stderr={result.stderr.strip()}')
        assert result.returncode != 0 and marker not in result.stdout + result.stderr
    result = run(f'cat {shlex.quote(str(cache / "readable"))}')
    print(f'runtime read: exit={result.returncode} output={result.stdout.strip()}')
    assert result.returncode == 0 and result.stdout == 'runtime-control'
    result = run(f'touch {shlex.quote(str(cache / "new"))}')
    print(f'runtime write denied: exit={result.returncode} stderr={result.stderr.strip()}')
    assert result.returncode != 0 and marker not in result.stdout + result.stderr


def test_real_bwrap_setup_failure_triggers_reprobe(monkeypatch):
    from tools import linux_sandbox_probe, shell_sandbox
    result = subprocess.run([shutil.which('bwrap'), '--invalid-corsia-test-option'],
                            capture_output=True, timeout=20)
    assert result.returncode == 1 and result.stderr.startswith(b'bwrap: ')
    calls = []
    monkeypatch.setattr(linux_sandbox_probe, 'invalidate_sandbox_probe', lambda: calls.append(True))
    shell_sandbox.recheck_after_launch_error(result.returncode, result.stderr)
    assert calls == [True]


def test_runtime_cache_is_readable_but_cannot_be_replaced(linux_box):
    box = linux_box
    cache = box.home / '.cache/corsia'
    binary = cache / 'runtimes/node/bin/node'
    assert not cache.exists()
    box.policy = ProtectedHostPathPolicy(box.home, box.state, write_roots=(cache,))
    env = dict(box.env, PATH=f'{binary.parent}:/usr/bin:/bin')
    def run(command):
        argv = build_protected_argv(command, box.workspace, path_policy=box.policy,
            rw_roots=[box.home, box.workspace], ro_roots=[], readonly_overlays=[], env=env,
            thread_id='one', executable=shutil.which('bwrap'))
        return subprocess.run(argv, capture_output=True, text=True, timeout=20)
    result = run(f'touch {shlex.quote(str(cache / "forged"))}')
    print(f'first-run cache write: exit={result.returncode} stderr={result.stderr.strip()}')
    assert cache.is_dir() and result.returncode != 0
    assert not (cache / 'forged').exists()
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho runtime-ok\n')
    binary.chmod(0o700)
    result = run('node')
    print(f'runtime execution: exit={result.returncode} output={result.stdout.strip()}')
    assert result.returncode == 0 and result.stdout.strip() == 'runtime-ok', result.stderr
    for path in (cache / 'runtimes', cache, cache.parent):
        result = run(f'mv {shlex.quote(str(path))} {shlex.quote(str(path))}.moved')
        print(f'rename {path.relative_to(box.home)}: exit={result.returncode} stderr={result.stderr.strip()}')
        assert result.returncode != 0, str(path)
    result = run(f'touch {shlex.quote(str(cache / "runtimes/new"))}')
    print(f'runtime write: exit={result.returncode} stderr={result.stderr.strip()}')
    assert result.returncode != 0


def test_session_and_user_keyrings_are_isolated(linux_box):
    # The driver gets its own session; its uniquely named @u key is unlinked
    # in a finally block. No keyctl program is required in the VM.
    import json
    import uuid
    marker = 'corsia-test-' + uuid.uuid4().hex
    child = r'''
import ctypes, errno, platform, sys
lib = ctypes.CDLL(None, use_errno=True)
lib.syscall.restype = ctypes.c_long
keyctl, add = {'x86_64': (250, 248), 'aarch64': (219, 217)}[platform.machine()]
def call(n, *args):
    return lib.syscall(ctypes.c_long(n), *args)
def search(ring, name):
    return call(keyctl, ctypes.c_long(10), ctypes.c_long(ring), ctypes.c_char_p(b'user'), ctypes.c_char_p(name), ctypes.c_long(0))
marker = sys.argv[1].encode()
def absent(ring, name):
    assert search(ring, name) == -1 and ctypes.get_errno() == errno.ENOKEY, (ring, ctypes.get_errno())
absent(-3, marker)
absent(-4, marker)
if sys.argv[2] == 'add':
    assert call(add, ctypes.c_char_p(b'user'), ctypes.c_char_p(marker + b'-child'), ctypes.c_char_p(b'x'), ctypes.c_size_t(1), ctypes.c_long(-4)) >= 0
else:
    absent(-4, marker + b'-child')
'''
    command = lambda mode: shlex.join(['/usr/bin/python3', '-c', child, marker, mode])
    argvs = [linux_box.argv_for(command(mode)) for mode in ('add', 'check')]
    driver = r'''
import ctypes, json, platform, subprocess, sys
lib = ctypes.CDLL(None, use_errno=True)
lib.syscall.restype = ctypes.c_long
keyctl, add = {'x86_64': (250, 248), 'aarch64': (219, 217)}[platform.machine()]
def call(n, *args): return lib.syscall(ctypes.c_long(n), *args)
marker = sys.argv[1].encode()
assert call(keyctl, ctypes.c_long(1), ctypes.c_void_p(None), ctypes.c_long(0), ctypes.c_long(0), ctypes.c_long(0)) >= 0
user_key = None
try:
    for ring in (-3, -4):
        key = call(add, ctypes.c_char_p(b'user'), ctypes.c_char_p(marker), ctypes.c_char_p(b'x'), ctypes.c_size_t(1), ctypes.c_long(ring))
        assert key >= 0, ctypes.get_errno()
        if ring == -4: user_key = key
        found = call(keyctl, ctypes.c_long(10), ctypes.c_long(ring), ctypes.c_char_p(b'user'), ctypes.c_char_p(marker), ctypes.c_long(0))
        assert found == key, (ring, ctypes.get_errno())
    for argv in json.loads(sys.argv[2]):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
finally:
    if user_key is not None:
        assert call(keyctl, ctypes.c_long(9), ctypes.c_long(user_key), ctypes.c_long(-4), ctypes.c_long(0), ctypes.c_long(0)) == 0
'''
    result = subprocess.run([sys.executable, '-I', '-c', driver, marker, json.dumps(argvs)],
                            capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('path', ['repo', 'repo/.git', 'repo/.git/logs',
                                  '.corsia', '.corsia/context', '.corsia/skills'])
@pytest.mark.parametrize('operation', ['mv', 'rmdir'])
def test_linux_each_ancestor_cannot_move_or_be_removed(linux_box, path, operation):
    box = linux_box
    source = shlex.quote(path)
    command = f'mv {source} {source}.moved' if operation == 'mv' else f'rmdir {source}'
    result = box.run(command)
    assert result.returncode != 0, result.stdout + result.stderr
    if operation == 'mv':
        assert 'Device or resource busy' in result.stderr
    assert (box.repo / '.git/HEAD').read_text() == 'intact\n'
    assert (box.workspace / '.corsia/context/A.md').read_text() == 'intact\n'


def test_linux_workspace_writes_private_reads_base_writes_and_namespaces(linux_box):
    box = linux_box
    private = [box.home / 'Documents/private', box.home / '.ssh/id_ed25519',
               box.state / 'keys/private']
    for path in private:
        result = box.run(f'cat {shlex.quote(str(path))}')
        assert result.returncode != 0
        assert 'fixture-private' not in result.stdout
    for path in ('repo/.git/HEAD', '.corsia/context/A.md', 'base/new'):
        result = box.run(f'echo bad > {shlex.quote(path)}')
        assert result.returncode != 0
    result = box.run('echo allowed > output; cat output')
    assert result.returncode == 0 and result.stdout == 'allowed\n'
    result = box.run('unshare -U true')
    assert result.returncode != 0
    host = subprocess.Popen(['sleep', '20'])
    try:
        result = box.run(f'kill -0 {host.pid}')
        assert result.returncode != 0
    finally:
        host.terminate()
        host.wait(timeout=3)


def test_linux_home_and_run_add_dirs_keep_persistence_and_ipc_hidden(linux_box):
    box = linux_box
    before = sorted(path.relative_to(box.home) for path in box.home.rglob('*'))
    modes = {path: (box.home / path).stat().st_mode & 0o777 for path in before
             if (box.home / path).is_dir()}
    for path in (box.state / 'keys/private', box.home / '.bashrc',
                 box.home / '.config/autostart', box.home / '.config/systemd/user'):
        result = box.run(f'cat {shlex.quote(str(path))}', home_grant=True)
        assert result.returncode != 0
    for command in ('ls /run', 'ls /run/systemd', 'cat /run/user/501/private',
                    'cat /run/dbus/system_bus_socket', 'cat /run/systemd/private',
                    'cat /var/run/systemd/private', 'echo bad > "$HOME/.zshrc"'):
        result = box.run(command, home_grant=True)
        assert result.returncode != 0, command
    # Only backing paths and the declared cache directories may be created.
    after = sorted(path.relative_to(box.home) for path in box.home.rglob('*')
                   if not path.is_relative_to(box.state / 'sandbox-tmp')
                   and not path.is_relative_to(box.state / 'sandbox-masks'))
    before = [path for path in before if not path.is_relative_to(Path('.local/state/corsia/sandbox-tmp'))
              and not path.is_relative_to(Path('.local/state/corsia/sandbox-masks'))]
    caches = {Path(value) for value in HOME_CACHES}
    expected = {part for cache in caches for part in (cache, *cache.parents) if part != Path('.')}
    from tools.managed_runtime import application_cache_root
    expected.add(application_cache_root(box.home).relative_to(box.home))
    created = expected - set(before)
    assert after == sorted(set(before) | created)
    for path in created:
        directory = box.home / path
        assert directory.is_dir()
        assert directory.stat().st_uid == os.getuid()
        assert directory.stat().st_mode & 0o777 == 0o700
        children = {child.relative_to(box.home) for child in directory.iterdir()}
        assert children == {child for child in created if child.parent == path}
    for cache in caches:
        assert not list((box.home / cache).iterdir())
    for path, mode in modes.items():
        assert (box.home / path).stat().st_mode & 0o777 == mode



def test_linux_thread_tmp_is_private_persistent_and_cleaned(linux_box):
    box = linux_box
    assert box.run('echo mine > /tmp/marker', thread='one').returncode == 0
    assert box.run('cat /tmp/marker', thread='one').stdout == 'mine\n'
    assert box.run('cat /tmp/marker', thread='two').returncode != 0
    backing = thread_tmp_root(box.state, 'one')
    assert box.run(f'cat {shlex.quote(str(backing / "marker"))}',
                   thread='two', home_grant=True).returncode != 0
    cleanup_thread_tmp(box.state, 'one')
    assert not backing.exists()
    assert thread_tmp_root(box.state, 'two').is_dir()


def test_linux_runtime_grants_do_not_reopen_host_tmp_devices_or_proc(linux_box):
    box = linux_box
    marker = box.home.parent / 'host-only-marker'
    marker.write_text('host-only')
    roots = [Path('/tmp'), Path('/dev'), Path('/proc')]
    result = box.run(f'cat {shlex.quote(str(marker))}', extra_roots=roots)
    assert result.returncode != 0
    assert 'host-only' not in result.stdout
    assert box.run('echo private > /tmp/runtime-marker', extra_roots=roots).returncode == 0
    assert (thread_tmp_root(box.state, 'one') / 'runtime-marker').read_text() == 'private\n'
    assert box.run(f'test ! -e /dev/kmsg && test ! -d /proc/{os.getpid()}', extra_roots=roots).returncode == 0
    assert box.run('unshare -U true', extra_roots=roots).returncode != 0


@pytest.mark.asyncio
async def test_linux_prepared_envelope_has_no_profile(linux_box, monkeypatch):
    from tools import shell_envelope
    box = linux_box
    envelope = shell_envelope.execution_envelope_for_consumed_action(
        None, leased=False, thread_id='one', grant_policies=[], policies=[],
        agent_account=None, env=box.env,
        build_profile=lambda *a, **k: pytest.fail('Linux must not render Seatbelt'),
        build_argv=lambda *a, **k: pytest.fail('Linux must not launch Safehouse'))
    profile, argv = await shell_envelope.prepare_execution_envelope('echo real', box.workspace, envelope)
    assert profile is None
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout == 'real\n', result.stderr


@pytest.mark.asyncio
async def test_linux_bash_tool_runs_and_refuses_failed_startup(linux_box, monkeypatch, tmp_path):
    from core.turn_core import TurnIdentity
    from host_access.processes import ProcessOwner
    from tools import linux_sandbox_probe, shell
    box = linux_box
    monkeypatch.setattr(shell, 'PROCESS_OWNER', ProcessOwner(
        runtime_dir=tmp_path / 'bash-runtime', term_grace_seconds=0.2, kill_grace_seconds=2))
    identity = {'_thread_id': 'one', '_host_access_turn_identity': TurnIdentity('linux-real', 1)}
    tool = shell.BashTool(working_dir=str(box.workspace))
    linux_sandbox_probe.invalidate_sandbox_probe()
    result = await tool.execute('printf real-bash', **identity)
    assert result == 'real-bash', result
    cached = linux_sandbox_probe._success
    assert cached is not None
    for command in ('touch /etc/corsia-denial-probe',
                    "printf 'Permission denied (publickey)\\n' >&2; exit 1"):
        result = await tool.execute(command, **identity)
        assert 'this command was blocked by the protected-mode sandbox' not in result
        assert 'If blocked by protected scope' in result
        assert linux_sandbox_probe._success == cached
    result = await tool.execute('sleep 5', timeout=1, **identity)
    assert 'timed out' in result
    assert linux_sandbox_probe._success == cached
    await tool.execute("printf 'bwrap: x\\n' >&2; exit 1", **identity)
    assert linux_sandbox_probe._success is None  # A spoof causes only a harmless re-probe.
    assert linux_sandbox_probe.sandbox_error() is None
    for refusal in (linux_sandbox_probe._MISSING, linux_sandbox_probe._failure('fixture userns denial')):
        monkeypatch.setattr(linux_sandbox_probe, 'sandbox_error', lambda: refusal)
        result = await tool.execute('touch must-not-run', **identity)
        assert result == refusal
        assert not (box.workspace / 'must-not-run').exists()


@pytest.mark.asyncio
async def test_linux_script_uses_real_process_owner_and_refuses_missing_sandbox(linux_box, monkeypatch, tmp_path):
    from automation import script_sandbox
    from host_access.processes import ProcessOwner
    from tools import linux_sandbox_probe
    box = linux_box
    monkeypatch.setattr(script_sandbox, 'PROCESS_OWNER', ProcessOwner(
        runtime_dir=tmp_path / 'runtime', term_grace_seconds=0.2, kill_grace_seconds=2))
    script = box.workspace / 'script.sh'
    script.write_text('echo real-script\necho output > written\n')
    result = await script_sandbox.run_script('one', None, script,
                                            run_id='test', timeout_seconds=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'real-script\n'
    assert (box.workspace / 'written').read_text() == 'output\n'
    monkeypatch.setattr(linux_sandbox_probe, 'sandbox_error', lambda: linux_sandbox_probe._MISSING)
    with pytest.raises(script_sandbox.ScriptSandboxError, match='Nothing ran'):
        await script_sandbox.run_script('one', None, script, run_id='missing', timeout_seconds=20)
