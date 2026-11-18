"""CI-only probe of the existing authenticated-script private-root envelope.

This tests directory access, not a broker, request authorization, or a native turn.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration
THREAD = "private-root-proof"
PAYLOAD = b"invented-private-root-payload"

_PROBE = r'''
import json, pathlib, socket, sys
control, run, mode = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]

def connect(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(payload)

def attempt(fn):
    try:
        fn()
    except OSError as exc:
        return exc.errno
    return 0

def bind(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(path))

# All five operations must work in the same-thread root under this envelope.
assert (control / 'canary').read_text() == 'control'
(control / 'write').write_text('control')
(control / 'victim.sock').unlink()
bind(control / 'bound.sock')
connect(control / 'listener.sock', b'control')
results = {
    'read': attempt(lambda: (run / 'canary').read_text()),
    'connect': attempt(lambda: connect(run / 'listener.sock', b'invented-private-root-payload')),
    'write': attempt(lambda: (run / 'write').write_text('changed')),
    'unlink': attempt(lambda: (run / 'victim.sock').unlink()),
    'bind': attempt(lambda: bind(run / 'bound.sock')),
}
if mode == 'intended':
    assert (run / 'canary').read_text() == 'private canary'
print(json.dumps(results), flush=True)
'''


@pytest.fixture
def required_safehouse():
    if os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") != "1":
        pytest.skip("real managed connection proof requires CI Safehouse")
    assert sys.platform == "darwin", "required proof needs macOS Seatbelt"
    assert not os.environ.get("CORSIA_THREAD_ID"), "never run Safehouse in an agent shell"
    from tools import shell_guard
    assert shell_guard.shutil.which("safehouse"), "required Safehouse is unavailable"


@pytest.fixture
async def boundary(required_safehouse, isolated_db, monkeypatch):
    from host_access.processes import ProcessOwner
    from tools import sandbox_config, unix_socket_policy

    with tempfile.TemporaryDirectory(prefix="pr-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        workspace = root / "w"
        workspace.mkdir()
        monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
        monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
        monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(root))
        bind_root = unix_socket_policy.corsia_temp_root_for_bind(THREAD)
        with tempfile.TemporaryDirectory(prefix="r", dir=unix_socket_policy.corsia_temp_root()) as raw_run:
            run = Path(raw_run).resolve()
            assert run.parent == unix_socket_policy.corsia_temp_root()
            assert not run.is_relative_to(bind_root)
            owner = ProcessOwner(runtime_dir=root / "runtime", term_grace_seconds=0.2)
            terminals, callback_errors = {}, []

            async def transition(record, state):
                try:
                    if state == "terminal":
                        terminals.setdefault(record.handle, asyncio.Event()).set()
                except BaseException as exc:
                    callback_errors.append(exc)
                    raise

            owner.configure_transition_callback(transition)
            try:
                yield SimpleNamespace(root=root, workspace=workspace, bind_root=bind_root,
                                      run=run, owner=owner, terminals=terminals,
                                      callback_errors=callback_errors)
            finally:
                await asyncio.wait_for(owner.shutdown(), timeout=10)
                assert owner.active_process_count() == 0
                assert not callback_errors, callback_errors
        assert not run.exists()
    assert not root.exists()


async def _launch(boundary, background, intended, control):
    from automation.script_sandbox import _script_profile
    from connections.github_agent import AgentAccountView
    from tools import shell_guard

    program = boundary.workspace / "probe.py"
    program.write_text(_PROBE)
    command = shlex.join(["/usr/bin/python3", str(program), str(control), str(boundary.run),
                          "intended" if intended else "sibling"])
    profile = boundary.root / "profile.sb"
    builder = _script_profile(AgentAccountView(state="not_installed", absent=True), boundary.run) if intended else shell_guard.execution_envelope_profile
    profile.write_text(builder(command, boundary.workspace, profile, leased=False, policies=[], thread_id=THREAD))
    try:
        result = await boundary.owner.spawn(
            shell_guard.safehouse_argv(command, boundary.workspace, profile, policies=[], thread_id=THREAD),
            thread_id=THREAD, turn_id="fixture-turn", cwd=boundary.workspace,
            env=shell_guard.safehouse_subprocess_env(THREAD), timeout_seconds=15, background=background,
        )
        if not background:
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            output = result.stdout
        else:
            terminal = boundary.terminals.setdefault(result.handle, asyncio.Event())
            await asyncio.wait_for(terminal.wait(), timeout=20)
            record = boundary.owner.status(result.handle, thread_id=THREAD)
            assert record.terminal_state == "exited" and record.exit_code == 0, record.log_path.read_text()
            output = record.log_path.read_bytes()
        assert not boundary.callback_errors, boundary.callback_errors
        return json.loads(output)
    finally:
        profile.unlink(missing_ok=True)


def _listener(path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    listener.setblocking(False)
    return listener


async def _receive(listener):
    loop = asyncio.get_running_loop()
    conn, _ = await asyncio.wait_for(loop.sock_accept(listener), timeout=5)
    try:
        conn.setblocking(False)
        return await asyncio.wait_for(loop.sock_recv(conn, 1024), timeout=5)
    finally:
        conn.close()


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.asyncio
async def test_private_root_grant_and_same_thread_sibling(boundary, background):
    run = boundary.run
    (run / "canary").write_text("private canary")
    run_listener = _listener(run / "listener.sock")
    victim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    victim.bind(str(run / "victim.sock"))
    try:
        for intended in (True, False):
            control = boundary.bind_root / ("intended" if intended else "sibling")
            control.mkdir()
            (control / "canary").write_text("control")
            control_listener = _listener(control / "listener.sock")
            control_victim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            control_victim.bind(str(control / "victim.sock"))
            try:
                results = await _launch(boundary, background, intended, control)
                assert await _receive(control_listener) == b"control"
                assert set(results) == {"read", "connect", "write", "unlink", "bind"}
                if intended:
                    assert results["read"] == results["connect"] == 0, results
                    assert await _receive(run_listener) == PAYLOAD
                else:
                    assert results["read"] in {errno.EACCES, errno.EPERM}, results
                    assert results["connect"] in {errno.EACCES, errno.EPERM}, results
                    # The launch has terminated: any successful sibling connect is queued.
                    with pytest.raises(BlockingIOError):
                        run_listener.accept()
                assert all(results[op] in {errno.EACCES, errno.EPERM}
                           for op in ("write", "unlink", "bind")), results
                assert (run / "canary").read_text() == "private canary"
                assert not (run / "write").exists()
                assert (run / "victim.sock").exists()
                assert not (run / "bound.sock").exists()
            finally:
                control_victim.close()
                control_listener.close()
    finally:
        victim.close()
        run_listener.close()
