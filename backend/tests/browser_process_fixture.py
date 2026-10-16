"""Real daemon processes; CLI executable/version and socket transport are explicit seams."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import psutil
import pytest
from host_access import browser_cdp, browser_cli, browser_identity as identity, browser_sessions as sessions

# Bounds only a child that never starts; a healthy one returns at once. Loaded CI runners took >3 s (#2679).
CHILD_READY_TIMEOUT = 30
# Bounds only a process that never exits or a close that never finishes: real process waits, CLI
# commands expected to return, and the owner's close_timeout once a test stops forcing a timeout.
# A stalled runner took >3 s to exit a closed daemon (#2871 run 36502971912).
EXIT_TIMEOUT = 30

DAEMON = '''
import json, os, pathlib, signal, socket, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
session = sys.argv[2]
time.sleep(float(os.environ.get('TEST_START_DELAY', '0')))
paths = [root / (session + suffix) for suffix in ('.pid', '.version', '.sock')]
paths[2].touch()  # Explicit endpoint transport seam under protected execution.
paths[0].write_text(str(os.getpid()))
paths[1].write_text('0.27.0')
child = None
if os.environ.get('TEST_CHILD'):
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    (root / (session + '.child')).write_text(str(child.pid))
def stop(*args):
    if child is not None:
        child.terminate()
        child.wait(timeout=EXIT_TIMEOUT)
    for path in paths:
        path.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print('ready', flush=True)
for line in sys.stdin:
    request = json.loads(line)
    assert request['action'] == 'close'
    time.sleep(float(os.environ.get('TEST_CLOSE_DELAY', '0')))
    if child is not None:
        child.terminate()
        child.wait(timeout=EXIT_TIMEOUT)
    print(json.dumps({'id': request['id'], 'success': True, 'data': {'closed': True}}), flush=True)
    if os.environ.get('TEST_HOLD_EXIT'):
        sys.stdin.readline()  # Exits only on the test's explicit release line, or EOF at teardown.
    stop()
stop()
'''.replace('EXIT_TIMEOUT', repr(EXIT_TIMEOUT))


class PipeReader:
    def __init__(self, child):
        self.child = child

    async def readline(self):
        return await asyncio.to_thread(self.child.stdout.readline)


class PipeWriter:
    def __init__(self, child):
        self.child = child

    def write(self, data):
        self.child.stdin.write(data)
        self.child.stdin.flush()

    def get_extra_info(self, name):
        assert name == 'socket'
        return self.child

    async def drain(self):
        pass

    def close(self):
        pass  # Producer must retain the pipe during its post-ack cleanup.

    async def wait_closed(self):
        pass


class SyntheticBrowser:
    def __init__(self, owner):
        self.owner = owner
        self.children = {}
        self.all_children = []
        self.env_changes = {}
        self.code = DAEMON
        self.calls = []
        self.result = sessions.CommandResult(0, 'ok', '')
        self.real_socket = False
        self.socket_connect = asyncio.open_unix_connection
        self.peer_identity = identity.connected_peer_pid

    def spawn(self, session, *, env_changes=None, code=DAEMON):
        env = dict(os.environ, AGENT_BROWSER_DAEMON='1', AGENT_BROWSER_SESSION=session,
                   AGENT_BROWSER_SOCKET_DIR=str(self.owner.socket_dir))
        env.update(env_changes or {})
        child = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.owner.socket_dir), session],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.children[session] = child
        self.all_children.append(child)
        return child

    async def invoke(self, argv, *, timeout):
        if argv[-1] == '--version':
            return sessions.CommandResult(0, 'agent-browser 0.27.0', '')
        assert 'close' not in argv and '--namespace' not in argv
        self.calls.append(tuple(argv))
        session = argv[argv.index('--session') + 1]
        if session not in self.children:
            child = self.spawn(session, env_changes=self.env_changes, code=self.code)
            await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), CHILD_READY_TIMEOUT)
        return self.result

    def release_exit(self, session):
        """Let a daemon started with TEST_HOLD_EXIT finish exiting after its close acknowledgement."""
        child = self.children[session]
        child.stdin.write(b'release\n')
        child.stdin.flush()

    async def connect(self, path, **kwargs):
        if self.real_socket:
            return await self.socket_connect(path, **kwargs)
        child = self.children[Path(path).stem]
        return PipeReader(child), PipeWriter(child)

    def peer_pid(self, peer):
        if isinstance(peer, subprocess.Popen):
            return peer.pid  # Pipe fixture's connected child; real sockets use kernel credentials.
        return self.peer_identity(peer)

    def finish(self):
        for child in self.all_children:
            if not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                try:
                    child.wait(timeout=EXIT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=EXIT_TIMEOUT)
            child.stdout.close()


@pytest.fixture
def synthetic_browser(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='mb-', dir='/tmp'))
    monkeypatch.setattr(identity, 'default_socket_root', lambda: root)
    monkeypatch.setattr(identity, 'socket_artifact_mode', stat.S_ISREG)
    owner = sessions.BrowserSessionOwner(namespace='corsia-test', binary=psutil.Process().exe())
    fake = SyntheticBrowser(owner)
    monkeypatch.setattr(owner, '_invoke', fake.invoke)
    monkeypatch.setattr(asyncio, 'open_unix_connection', fake.connect)
    monkeypatch.setattr(identity, 'connected_peer_pid', fake.peer_pid)
    # The synthetic daemon has no page: lifecycle tests see a page without a password field
    # (the guard has its own tests) and the host's installed Chrome never reaches the argv.
    async def synthetic_page(_run):
        return 'ws://synthetic.test/devtools/page/1'

    async def no_password_field(endpoint, _visit, _known):
        return browser_cdp.PasswordProbe('synthetic-visit', 'synthetic.test', frozenset(), endpoint)
    monkeypatch.setattr(browser_cdp, 'page_endpoint', synthetic_page)
    monkeypatch.setattr(browser_cdp, 'page_password_probe', no_password_field)
    monkeypatch.setattr(browser_cli, 'chrome_executable', lambda: (None, 'default'))
    yield fake
    fake.finish()
    shutil.rmtree(root)
