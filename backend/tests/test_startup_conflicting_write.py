"""A conflicting external edit must not prevent the backend from starting."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_durable_writes import durable_scope as durable_scope

BACKEND = Path(__file__).resolve().parents[1]
WORKER = r'''
import sys, threading
from pathlib import Path
from infra import config, database
from infra.platform_paths import WorkspaceSelection
from registry import durable_writes, service
workspace, db_path, owner, target, cut = sys.argv[1:]
workspace = Path(workspace)
config.WORKSPACE = workspace
config.WORKSPACE_SELECTION = WorkspaceSelection(workspace_id='workspace-stable-id', root=workspace, name='Test')
config.DB_PATH = database.DB_PATH = Path(db_path)
service.get_allowed_dirs_rw = lambda **kwargs: [workspace.resolve()]
def pause(*args, **kwargs):
    print('FILE_APPLIED', flush=True)
    threading.Event().wait()
if cut == 'applied':
    durable_writes._register_applied = pause
else:
    durable_writes.replace_bound_file = pause
durable_writes.write_text(Path(target), 'new bytes', thread_id=owner, project_id='project-alpha', workspace=workspace, update_trigger='test fixture')
'''
SERVER = r'''
import os, sys
from pathlib import Path
from infra import config, database
from infra.platform_paths import WorkspaceSelection
workspace, db_path, port = sys.argv[1:]
config.WORKSPACE = Path(workspace)
config.WORKSPACE_SELECTION = WorkspaceSelection(workspace_id='workspace-stable-id', root=Path(workspace), name='Test')
config.DB_PATH = database.DB_PATH = Path(db_path)
if os.environ.get('TEST_FAIL_REGISTER'):
    # Only this late registration failure is mocked; startup and the next intent are real.
    from registry import durable_writes
    original = durable_writes._register_applied
    def fail_one(shared, *args, **kwargs):
        if str(shared.path) == os.environ['TEST_FAIL_REGISTER']:
            raise ValueError('injected registration failure')
        return original(shared, *args, **kwargs)
    durable_writes._register_applied = fail_one
import uvicorn
uvicorn.run('main:app', host='127.0.0.1', port=int(port), log_level='info')
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def start_server(workspace, db_path, env, port, log):
    return subprocess.Popen([sys.executable, '-c', SERVER, str(workspace), str(db_path), str(port)], cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_ready(proc, port, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and proc.poll() is None:
        with socket.socket() as sock:
            sock.settimeout(.2)
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                return True
        time.sleep(.1)
    return False


@pytest.mark.parametrize('failure', ['mismatch', 'registration'])
def test_startup_survives_one_failed_recovery(durable_scope, tmp_path, failure):
    workspace, project_root, owner, db = durable_scope
    db_path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    # isolated_db creates the current schema directly, without startup's version marker.
    from infra.schema import SCHEMA_VERSION
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    db.commit()
    state = tmp_path / 'state'
    state.mkdir()
    env = {**os.environ, 'CORSIA_WORKSPACE': str(workspace), 'CORSIA_STATE_DIR': str(state), 'CORSIA_TEST_MODE': '1', 'CORSIA_ANALYTICS_DISABLE': '1', 'PYTHONPATH': str(BACKEND)}
    port = free_port()
    log_path = tmp_path / 'startup.log'
    with log_path.open('w+') as log:
        server = start_server(workspace, db_path, env, port, log)
        try:
            assert wait_ready(server, port), f'control startup failed: {log_path.read_text()}'
        finally:
            server.terminate()
            server.wait(timeout=15)
    targets = [project_root / 'shared' / 'planning' / 'conflict.md', project_root / 'shared' / 'planning' / 'other.md']
    for target, cut in zip(targets, ('applied', 'staged')):
        target.write_text('old bytes')
        writer = subprocess.Popen([sys.executable, '-c', WORKER, str(workspace), str(db_path), owner, str(target), cut], cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert writer.stdout.readline().strip() == 'FILE_APPLIED'
        finally:
            writer.kill()
            writer.wait(timeout=10)
    rows = db.execute('SELECT * FROM durable_write_intents').fetchall()
    by_target = {row['target_path']: row for row in rows}
    held_intent = dict(by_target[str(targets[0])])
    assert len(rows) == 2 and by_target[str(targets[0])]['phase'] == 'file_applied' and by_target[str(targets[1])]['phase'] == 'staged'
    stage = Path(by_target[str(targets[0])]['stage_path'])
    assert not stage.exists()
    backup = Path(by_target[str(targets[0])]['backup_path'])
    assert backup.read_text() == 'old bytes'
    if failure == 'mismatch':
        targets[0].write_text('outside edit')
    else:
        env['TEST_FAIL_REGISTER'] = str(targets[0])
    expected = 'outside edit' if failure == 'mismatch' else 'new bytes'
    with log_path.open('w+') as log:
        server = start_server(workspace, db_path, env, port, log)
        try:
            assert wait_ready(server, port), f'startup did not complete: {log_path.read_text()[-3000:]}'
        finally:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=15)
    assert targets[0].read_text() == expected
    assert backup.read_text() == 'old bytes'
    rows = db.execute('SELECT * FROM durable_write_intents').fetchall()
    assert [dict(row) for row in rows] == [held_intent]
    assert targets[1].read_text() == 'old bytes'
    assert not stage.exists()
    log_text = log_path.read_text()
    assert 'ERROR' in log_text and str(targets[0]) in log_text
    assert ('does not match its target bytes' if failure == 'mismatch' else 'injected registration failure') in log_text
    with log_path.open('w+') as log:
        server = start_server(workspace, db_path, env, port, log)
        try:
            assert wait_ready(server, port), f'second startup did not complete: {log_path.read_text()[-3000:]}'
        finally:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=15)
    assert targets[0].read_text() == expected and backup.read_text() == 'old bytes'
    assert not stage.exists()
    assert dict(db.execute('SELECT * FROM durable_write_intents').fetchone()) == held_intent
