"""AF_UNIX producer proof on a permitted runner; bind failures are never skipped."""
import asyncio
import stat

import pytest
from host_access import browser_identity as identity, browser_sessions as sessions
from tests.browser_process_fixture import synthetic_browser, DAEMON, EXIT_TIMEOUT  # noqa: F401

pytestmark = pytest.mark.integration
SOCKET_DAEMON = DAEMON.replace(
    'paths[2].touch()  # Explicit endpoint transport seam under protected execution.',
    'sock = socket.socket(socket.AF_UNIX)\nsock.bind(str(paths[2])); sock.listen()',
).replace(
    'for line in sys.stdin:',
    "stream, _ = sock.accept()\nfor line in stream.makefile('r'):",
).replace(
    "    print(json.dumps({'id': request['id'], 'success': True, 'data': {'closed': True}}), flush=True)",
    "    stream.sendall((json.dumps({'id': request['id'], 'success': True, 'data': {'closed': True}}) + '\\n').encode())",
)


async def test_actual_socket_slow_ack_and_kernel_peer_identity(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    monkeypatch.setattr(identity, 'socket_artifact_mode', stat.S_ISSOCK)
    fake.real_socket = True
    fake.code = SOCKET_DAEMON
    fake.env_changes = {'TEST_CLOSE_DELAY': '0.3', 'TEST_CHILD': '1'}
    fake.owner.close_timeout = 0.05
    await fake.owner.run('thread', ['snapshot'])
    runtime = fake.owner._sessions['thread']
    record = runtime.record
    stamp = identity.artifact_stamp(fake.owner.socket_dir / f'{record.session}.sock', socket_file=True)
    await asyncio.sleep(1.15)
    await fake.owner.run('thread', ['snapshot'])
    assert identity.artifact_stamp(fake.owner.socket_dir / f'{record.session}.sock', socket_file=True) == stamp
    with pytest.raises(sessions.BrowserSessionCleanupError, match='pending'):
        await fake.owner.close_thread('thread')
    pending = runtime.close_task
    assert fake.children[record.session].poll() is None
    await asyncio.wait_for(asyncio.shield(pending), EXIT_TIMEOUT)
    await asyncio.to_thread(fake.children[record.session].wait, EXIT_TIMEOUT)
    await fake.owner.close_thread('thread')
    assert fake.owner.live_count() == 0
    assert identity.session_absent(fake.owner.socket_dir, record.session)
    fake.env_changes = {}
    fake.owner.close_timeout = EXIT_TIMEOUT
    await fake.owner.run('thread', ['snapshot'])
    await fake.owner.shutdown()
    await fake.owner.shutdown()


async def test_actual_connected_foreign_peer_rejected(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    monkeypatch.setattr(identity, 'socket_artifact_mode', stat.S_ISSOCK)
    fake.real_socket = True
    fake.code = SOCKET_DAEMON
    await fake.owner.run('thread', ['snapshot'])
    await fake.owner.run('foreign', ['snapshot'])
    foreign = fake.owner._sessions['foreign'].record

    async def wrong_endpoint(path, **kwargs):
        return await fake.socket_connect(str(fake.owner.socket_dir / f'{foreign.session}.sock'), **kwargs)

    monkeypatch.setattr(asyncio, 'open_unix_connection', wrong_endpoint)
    with pytest.raises(sessions.BrowserSessionCleanupError, match='connected peer identity changed'):
        await fake.owner.close_thread('thread')
    assert fake.owner.live_count() == 2
    # EOF can terminate the test daemon; no successful close was sent or certified.
