"""Capability probe for a proposed run-private connection, not a broker.

CI-only: actual ProcessOwner + generated Safehouse profiles, invented bytes and
fresh roots. Directory assertions deliberately expose an unsupported boundary;
there is no xfail and no hand-authored deny rule to manufacture enforcement.
Kernel observations below do not implement request authorization or prove a
foreground pre-exec gate. Those seams do not exist on the baseline.
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
THREAD = "managed-connection-proof"
PAYLOAD = b"invented-observer-bytes"

_CONNECT = """
import socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(5)
    client.connect(sys.argv[1])
    assert client.recv(1) == b'G'
    client.sendall(b'invented-observer-bytes')
    client.shutdown(socket.SHUT_WR)
    assert client.recv(1) == b'D'
"""

_DIRECTORY = """
import json, pathlib, socket, sys
control, run = map(pathlib.Path, sys.argv[1:])
# Prove each operation works in the ordinary same-thread root first.
(control / 'canary').write_text('control')
(control / 'canary').unlink()
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    listener.bind(str(control / 'control.sock'))
(control / 'control.sock').unlink()
results = {}
for operation in ('write', 'unlink', 'bind'):
    try:
        if operation == 'write':
            (run / 'canary').write_text('changed')
        elif operation == 'unlink':
            (run / 'victim.sock').unlink()
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(run / 'squatter.sock'))
    except OSError as exc:
        results[operation] = exc.errno
    else:
        results[operation] = 0
print(json.dumps(results), flush=True)
"""


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

    with tempfile.TemporaryDirectory(prefix="mc-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        workspace = root / "w"
        workspace.mkdir()
        monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
        monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
        monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(root))
        bind_root = unix_socket_policy.corsia_temp_root_for_bind(THREAD)
        run = bind_root / "run"
        run.mkdir(mode=0o700)
        owner = ProcessOwner(runtime_dir=root / "runtime", term_grace_seconds=0.2)
        terminals = {}

        async def transition(record, state):
            # A disposable persistence sink, not a fake launch or membership check.
            if state == "terminal":
                terminals.setdefault(record.handle, asyncio.Event()).set()

        owner.configure_transition_callback(transition)
        try:
            yield SimpleNamespace(root=root, workspace=workspace, bind_root=bind_root,
                                  run=run, owner=owner, terminals=terminals)
        finally:
            await asyncio.wait_for(owner.shutdown(), timeout=10)
            assert owner.active_process_count() == 0
    assert not root.exists()


async def _launch(boundary, script, arguments, background):
    from tools import shell_guard

    program = boundary.workspace / "probe.py"
    program.write_text(script)
    # The pytest venv lives under HOME on hosted runners, which the generated
    # profile denies. These probes use only the macOS system Python's stdlib.
    command = shlex.join(["/usr/bin/python3", str(program), *map(str, arguments)])
    profile = boundary.root / "profile.sb"
    profile.write_text(shell_guard.execution_envelope_profile(
        command, boundary.workspace, profile, leased=False, policies=[], thread_id=THREAD,
    ))
    result = await boundary.owner.spawn(
        shell_guard.safehouse_argv(command, boundary.workspace, profile, policies=[], thread_id=THREAD),
        thread_id=THREAD, turn_id="fixture-turn", cwd=boundary.workspace,
        env=shell_guard.safehouse_subprocess_env(THREAD), timeout_seconds=15,
        background=background,
    )
    if not background:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return result.record, result.stdout
    terminal = boundary.terminals.setdefault(result.handle, asyncio.Event())
    record = boundary.owner.status(result.handle, thread_id=THREAD)
    if record.terminal_state == "running":
        await asyncio.wait_for(terminal.wait(), timeout=20)
    record = boundary.owner.status(result.handle, thread_id=THREAD)
    assert record.terminal_state == "exited" and record.exit_code == 0, record.log_path.read_text()
    return record, record.log_path.read_bytes()


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.asyncio
async def test_real_launch_connects_and_observer_sees_owned_kernel_peer(boundary, background):
    from host_access.browser_identity import connected_peer_pid
    from host_access.processes import _process_creation_fingerprint

    observed = asyncio.get_running_loop().create_future()
    handlers = set()

    async def observe(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            peer = writer.get_extra_info("socket")
            pid = connected_peer_pid(peer)
            records = boundary.owner.running_records()
            assert len(records) == 1
            record = records[0]
            # Safehouse may exec or retain wrappers; the connector need not be
            # the group leader. Observe its actual group and owning session.
            group = os.getpgid(pid)
            assert os.getsid(pid) == record.pgid
            birth = _process_creation_fingerprint(pid)
            assert _process_creation_fingerprint(record.pid) == record.creation_fingerprint
            await asyncio.sleep(0)  # re-observe across a scheduling boundary
            assert connected_peer_pid(peer) == pid
            assert os.getsid(pid) == record.pgid and os.getpgid(pid) == group
            assert _process_creation_fingerprint(pid) == birth
            assert _process_creation_fingerprint(record.pid) == record.creation_fingerprint
            writer.write(b"G")
            await writer.drain()
            payload = await asyncio.wait_for(reader.read(), timeout=5)
            assert payload == PAYLOAD  # nonzero observer control
            writer.write(b"D")
            await writer.drain()
            observed.set_result(record.handle)
        except BaseException as exc:
            observed.set_exception(exc)
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    path = boundary.run / "listener.sock"
    server = await asyncio.start_unix_server(observe, path=str(path))
    try:
        async with server:
            record, _ = await _launch(boundary, _CONNECT, [path], background)
            assert await asyncio.wait_for(observed, timeout=5) == record.handle
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*tuple(handlers), return_exceptions=True)
        if observed.done() and not observed.cancelled():
            observed.exception()  # retrieve callback failures even if launch failed
        else:
            observed.cancel()
        path.unlink(missing_ok=True)
    assert not path.exists()


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.asyncio
async def test_same_thread_sibling_cannot_write_unlink_or_bind_run_directory(boundary, background):
    """Expected failing regression: baseline grants the complete thread root."""
    control = boundary.bind_root / "control"
    control.mkdir()
    canary = boundary.run / "canary"
    canary.write_text("original")
    victim_path = boundary.run / "victim.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as victim:
        victim.bind(str(victim_path))
        _, output = await _launch(boundary, _DIRECTORY, [control, boundary.run], background)
        results = json.loads(output)
        assert set(results) == {"write", "unlink", "bind"}
        assert all(value in {errno.EACCES, errno.EPERM} for value in results.values()), (
            "generated policy lacks run-private directory protection: " + repr(results)
        )
        assert canary.read_text() == "original"
        assert victim_path.exists()
        assert not (boundary.run / "squatter.sock").exists()
