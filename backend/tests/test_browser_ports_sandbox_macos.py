"""Real Seatbelt proof for reserved managed-browser listeners and daemon sockets.

The listeners are disposable Python stubs; managed Chrome behavior has separate tests.
"""
import os
import socket
import subprocess
import sys

import pytest

from host_access import browser_ports
from tools.host_path_policy import ProtectedHostPathPolicy

_CONNECT = '''
import errno, socket, sys
try:
    with socket.socket(int(sys.argv[2]), socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect((sys.argv[3], int(sys.argv[1])))
except PermissionError as exc:
    print('denied', exc.errno)
    raise SystemExit(13)
'''
_HTTP = '''
import sys, urllib.error, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/json/version', timeout=2)
except urllib.error.URLError as exc:
    if isinstance(exc.reason, PermissionError):
        print('denied', exc.reason.errno)
        raise SystemExit(13)
    raise
'''
_WEBSOCKET = '''
import asyncio, sys, websockets
async def connect():
    try:
        async with websockets.connect('ws://127.0.0.1:'+sys.argv[1]+'/devtools/browser/fixture', open_timeout=2): pass
    except PermissionError as exc:
        print('denied', exc.errno)
        raise SystemExit(13)
asyncio.run(connect())
'''
_BIND = '''
import socket, sys
try:
    with socket.socket() as listener: listener.bind(('127.0.0.1', int(sys.argv[1])))
except PermissionError as exc:
    print('denied', exc.errno)
    raise SystemExit(13)
'''


def _run(profile, script, port, *args):
    return subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', script, str(port), *args],
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('leased', [False, True])
@pytest.mark.parametrize(('family', 'destination'), [
    (socket.AF_INET, '127.0.0.1'),
    (socket.AF_INET, '0.0.0.0'),
    (socket.AF_INET6, '::1'),
    (socket.AF_INET6, '::ffff:127.0.0.1'),
    (socket.AF_INET6, '::ffff:0.0.0.0'),
])
def test_reserved_cdp_and_stream_deny_connect_and_bind_with_control(tmp_path, leased, family, destination):
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state')
    rules = policy.hard_floor_sandbox_rules() if leased else policy.sandbox_rules()
    profile = '(version 1)(allow default)' + rules
    control = socket.socket(socket.AF_INET6 if destination == '::1' else socket.AF_INET)
    cdp, stream = browser_ports.reserve_pair()
    try:
        control.bind(('::1' if destination == '::1' else '127.0.0.1', 0))
        control.listen()
        control_port = control.getsockname()[1]
        assert control_port not in browser_ports.RESERVED_PORTS
        allowed = _run(profile, _CONNECT, control_port, str(family), destination)
        if allowed.returncode == 71 and 'sandbox_apply: Operation not permitted' in allowed.stderr:
            if os.environ.get('CORSIA_REQUIRE_SAFEHOUSE') == '1':
                pytest.fail('required sandbox could not be applied')
            pytest.skip('nested macOS sandbox unavailable in this execution environment')
        assert allowed.returncode == 0, allowed.stderr
        for listener in (cdp, stream):
            listener.listen()
            port = listener.getsockname()[1]
            # Backend/control connectivity remains available outside the agent profile.
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                pass
            if destination == '::1':
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as ipv6_listener:
                    ipv6_listener.bind(('::1', port))
                    ipv6_listener.listen()
                    refused = _run(profile, _CONNECT, port, str(family), destination)
                    assert refused.returncode == 13 and refused.stdout.strip() in {'denied 1', 'denied 13'}
            else:
                refused = _run(profile, _CONNECT, port, str(family), destination)
                assert refused.returncode == 13 and refused.stdout.strip() in {'denied 1', 'denied 13'}
            for script in (_HTTP, _WEBSOCKET):
                refused = _run(profile, script, port, str(family), destination)
                assert refused.returncode == 13 and refused.stdout.strip() in {'denied 1', 'denied 13'}
            listener.close()
            refused_bind = _run(profile, _BIND, port)
            assert refused_bind.returncode == 13 and refused_bind.stdout.strip() in {'denied 1', 'denied 13'}
        # Binding outside the reserved inventory still works.
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        free_port = probe.getsockname()[1]
        probe.close()
        assert free_port not in browser_ports.RESERVED_PORTS
        assert _run(profile, _BIND, free_port).returncode == 0
    finally:
        control.close()
        cdp.close()
        stream.close()
