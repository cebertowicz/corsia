"""Thread temp cleanup preserves retirement even when a removal fails."""
import sys
from types import SimpleNamespace

import pytest

from tools.linux_sandbox import cleanup_thread_tmp, thread_tmp_root


def test_cleanup_removes_mode_zero_directory(tmp_path):
    root = thread_tmp_root(tmp_path, 'one')
    locked = root / 'locked'
    locked.mkdir(parents=True)
    (locked / 'file').write_text('temporary')
    locked.chmod(0)
    try:
        cleanup_thread_tmp(tmp_path, 'one')
        assert not root.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)


def test_cleanup_removes_file_inside_read_only_directory(tmp_path):
    root = thread_tmp_root(tmp_path, 'one')
    locked = root / 'locked'
    locked.mkdir(parents=True)
    (locked / 'file').write_text('temporary')
    locked.chmod(0o500)
    try:
        cleanup_thread_tmp(tmp_path, 'one')
        assert not root.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)


@pytest.mark.asyncio
async def test_thread_close_continues_after_one_cleanup_failure(monkeypatch):
    from automation import managed_process_events as events
    from host_access import processes
    from infra import db_async
    from tools import linux_sandbox

    calls = []
    async def commit(token):
        calls.append('commit')
    def cleanup(_state, thread_id):
        calls.append(thread_id)
        if thread_id == 'first':
            raise PermissionError('fixture')
    async def run_in_db(function):
        assert function is events.drain_retirements
        calls.append('drain')
    def log(message, thread_id):
        calls.append(('logged', thread_id))

    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(processes, 'PROCESS_OWNER', SimpleNamespace(commit_thread_close=commit))
    monkeypatch.setattr(linux_sandbox, 'cleanup_thread_tmp', cleanup)
    monkeypatch.setattr(db_async, 'run_in_db', run_in_db)
    monkeypatch.setattr(events.logger, 'exception', log)
    await events.finish_thread_close(SimpleNamespace(thread_ids=('first', 'second')))
    assert calls == ['commit', 'first', ('logged', 'first'), 'second', 'drain']
