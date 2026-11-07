"""Claude Code launch settings respect workspace Git history boundaries."""

import asyncio
import json
import subprocess

from infra import config
from infra.database import get_db
from subagents.cc_git_snapshot import prepare_session_git_snapshot
from subagents.cc_stream import CCStreamSession


def _settings(cwd):
    session = CCStreamSession(binary='claude', session_id='session', working_dir=str(cwd))
    asyncio.run(prepare_session_git_snapshot(session, session.working_dir))
    cmd = session.build_cmd()
    return json.loads(cmd[cmd.index('--settings') + 1])


def test_workspace_git_snapshot_off_registered_worktree_unchanged(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'WORKSPACE', tmp_path)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    project = tmp_path / 'projects' / 'p'
    project.mkdir(parents=True)
    repo = project / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    worktree = repo / '.worktrees' / 'feature'
    worktree.mkdir(parents=True)
    gitdir = repo / '.git' / 'worktrees' / 'feature'
    gitdir.parent.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '--separate-git-dir', str(gitdir), str(worktree)], check=True)
    (gitdir / 'gitdir').write_text(f'{worktree / ".git"}\n')
    db = get_db()
    try:
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','p','t','t')")
        db.execute("INSERT INTO project_repo_policies VALUES ('p','repo',?,NULL,1)", (str(repo),))
        db.commit()
    finally:
        db.close()

    (project / '.git').mkdir()
    assert _settings(project)['includeGitInstructions'] is False
    assert 'includeGitInstructions' not in _settings(repo)
    assert 'includeGitInstructions' not in _settings(worktree)
    monkeypatch.chdir(project)
    session = CCStreamSession(binary='claude', session_id='session')
    asyncio.run(prepare_session_git_snapshot(session, session.working_dir))
    inherited = session.build_cmd()
    assert json.loads(inherited[inherited.index('--settings') + 1])['includeGitInstructions'] is False
    session = CCStreamSession(binary='claude', session_id='session', working_dir=str(project),
                              confinement='untrusted', mcp_config='{}')
    asyncio.run(prepare_session_git_snapshot(session, session.working_dir))
    confined = session.build_cmd()
    assert json.loads(confined[confined.index('--settings') + 1])['includeGitInstructions'] is False


def test_session_prepares_setting_once_off_event_loop(monkeypatch):
    import threading
    from subagents import managed_launch

    calls = []
    loop_thread = threading.get_ident()

    def probe(cwd):
        calls.append((cwd, threading.get_ident()))
        return True

    monkeypatch.setattr(managed_launch, 'workspace_git_snapshot_disabled', probe)
    session = CCStreamSession(binary='claude', session_id='session', working_dir='/workspace')

    async def prepare_twice():
        await prepare_session_git_snapshot(session, session.working_dir)
        await prepare_session_git_snapshot(session, session.working_dir)

    asyncio.run(prepare_twice())
    assert len(calls) == 1 and calls[0][0] == '/workspace' and calls[0][1] != loop_thread
    assert session.invocation().disable_git_snapshot is True


def test_legacy_checkpoint_then_resume_reuses_setting(isolated_db, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from core import fork_cc
    from history.types import HistoryBoundary
    from registry.cc_session import persist_cc_session
    from subagents import managed_launch
    from subagents.cc_invocation import ClaudeInvocation
    from subagents.cc_runtime_helpers import _artifact_cc_session_invocation

    seed, mcp, transcript = (tmp_path / name for name in ('seed', 'mcp', 'transcript'))
    for path in (seed, mcp, transcript):
        path.write_text('data')
    old = ClaudeInvocation(binary='claude', permission_mode='bypassPermissions', session_id='s',
                           seed_file=str(seed), mcp_config=str(mcp), working_dir='/workspace')
    db = get_db()
    db.execute("INSERT INTO threads(id,user_id,created_at,updated_at) VALUES ('thread','u','t','t')")
    db.commit()
    persist_cc_session('thread', 's', invocation=old, db=db)
    calls = []
    monkeypatch.setattr(managed_launch, 'workspace_git_snapshot_disabled', lambda cwd: calls.append(cwd) or True)
    monkeypatch.setattr(fork_cc, '_identity_from_live', lambda _live: (
        's', old, str(tmp_path), str(transcript), None, None, 'none'))

    async def checkpoint_then_resume():
        source = await fork_cc.capture_warm_source(
            'thread', HistoryBoundary(thread_id='thread', history_epoch=0, seq=1, message_id='m'),
            target=SimpleNamespace(harness='claude-code'), binary='claude', live=object())
        assert source is not None and source.invocation.disable_git_snapshot is True
        restored = _artifact_cc_session_invocation('thread', db)
        assert restored is not None and restored.disable_git_snapshot is True
        session = CCStreamSession(binary='claude', session_id='s', working_dir='/workspace')
        session.disable_git_snapshot = restored.disable_git_snapshot
        await prepare_session_git_snapshot(session, session.working_dir)
        assert session.invocation().disable_git_snapshot is True

    asyncio.run(checkpoint_then_resume())
    assert calls == ['/workspace']


def test_retired_or_replaced_session_uses_fresh_unstored_value(isolated_db, monkeypatch):
    import threading
    from registry.cc_session import persist_cc_session
    from registry.models import find_for_thread
    from subagents import managed_launch
    from subagents.cc_git_snapshot import ensure_registered_git_snapshot
    from subagents.cc_invocation import ClaudeInvocation, claude_invocation_metadata

    db = get_db()
    db.execute("INSERT INTO threads(id,user_id,created_at,updated_at) VALUES ('thread','u','t','t')")
    db.commit()
    old = ClaudeInvocation(binary='claude', permission_mode='bypassPermissions', session_id='old')
    current = ClaudeInvocation(binary='claude', permission_mode='bypassPermissions', session_id='new')
    loop_thread = threading.get_ident()
    probes = []
    monkeypatch.setattr(managed_launch, 'workspace_git_snapshot_disabled',
                        lambda cwd: probes.append(threading.get_ident()) or True)

    async def capture():
        assert await ensure_registered_git_snapshot('thread', 'old', old) is True
        assert find_for_thread('thread', 'claude-code', 'session', db=db) is None
        persist_cc_session('thread', 'old', db=db)
        before = find_for_thread('thread', 'claude-code', 'session', db=db)['metadata']
        assert await ensure_registered_git_snapshot('thread', 'old', old) is True
        assert find_for_thread('thread', 'claude-code', 'session', db=db)['metadata'] == before
        persist_cc_session('thread', 'new', invocation=current, db=db)
        assert await ensure_registered_git_snapshot('thread', 'old', old) is True
        row = find_for_thread('thread', 'claude-code', 'session', db=db)
        assert json.loads(row['metadata'])['claude_invocation'] == claude_invocation_metadata(current)

    try:
        asyncio.run(capture())
        assert len(probes) == 3 and all(thread != loop_thread for thread in probes)
    finally:
        db.close()


def test_command_builder_does_not_probe_git(monkeypatch):
    from subagents.cc_invocation import ClaudeInvocation, build_claude_cmd

    def forbidden(*args, **kwargs):
        raise AssertionError('command construction must not run git')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    invocation = ClaudeInvocation(binary='claude', permission_mode='bypassPermissions',
                                  working_dir='/workspace', disable_git_snapshot=True)
    command = build_claude_cmd(invocation, stream_input=True)
    assert json.loads(command[command.index('--settings') + 1])['includeGitInstructions'] is False


def test_live_and_restored_warm_fork_share_snapshot_setting(monkeypatch):
    from types import SimpleNamespace
    from core.fork_cc_commands import warm_command
    from subagents.cc_invocation import claude_invocation_from_metadata, claude_invocation_metadata

    session = CCStreamSession(binary='claude', session_id='session', working_dir='/workspace')
    session.disable_git_snapshot = True
    invocation = session.invocation()
    restored = claude_invocation_from_metadata(claude_invocation_metadata(invocation))
    assert restored is not None
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('must not recompute session setting')))
    live = session.build_cmd()
    kind = SimpleNamespace(step_limit=lambda *_args: None)
    request = SimpleNamespace(binary=None, mcp_config=None)
    fork = warm_command(SimpleNamespace(invocation=restored), request, kind, 'snapshot')
    for command in (live, fork):
        assert json.loads(command[command.index('--settings') + 1])['includeGitInstructions'] is False
