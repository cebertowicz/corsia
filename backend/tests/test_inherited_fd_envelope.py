"""Opt-in actual ProcessOwner/generated-envelope FD falsifier; invented bytes only.

Safehouse executes only in supported macOS CI. Linux requires its actual keyring
and bubblewrap chain. A denied or stripped descriptor is an assertion failure.
This is transport closure only; no policy or ProcessOwner resource lifecycle.
"""
from __future__ import annotations

import array
import asyncio
import os
from pathlib import Path
import shlex
import shutil
import socket
import sys
import tempfile

import pytest

pytestmark = pytest.mark.integration
THREAD = "inherited-fd-probe"

CHILD = '''
import socket, sys, time
from inherited_fd_probe_support import receive_one
try:
    control = socket.socket(fileno=int(sys.argv[1]))
    control.settimeout(5)
    assert control.family == socket.AF_UNIX
    assert control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
    control.getpeername()
    assert control.recv(1) == b'H'
    control.sendall(b'I')
    if sys.argv[2] == 'positive':
        stream = receive_one(control)
        stream.sendall(b'P')
        stream.close()
        control.close()
    else:
        try:
            receive_one(control)
        except ValueError:
            time.sleep(2)  # let the parent check rights closed before process exit
        else:
            raise AssertionError('malformed reply accepted')
except BaseException as exc:
    print('descriptor unavailable or denied:', repr(exc), file=sys.stderr, flush=True)
    raise
'''
SIBLING = '''
import os, socket, stat, sys
try:
    fd = int(sys.argv[1])
    identity = os.fstat(fd)
    expected = (int(sys.argv[2]), int(sys.argv[3]))
    if stat.S_ISSOCK(identity.st_mode) and (identity.st_dev, identity.st_ino) == expected:
        sock = socket.socket(fileno=fd)
        assert sock.family == socket.AF_UNIX
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
        sock.getpeername()
        print('ATTACHED', flush=True)
        sock.sendall(b'S')
        sys.exit(86)
except (OSError, ValueError):
    pass
'''


def test_probe_programs_parse():
    compile(CHILD, "child.py", "exec")
    compile(SIBLING, "sibling.py", "exec")


@pytest.fixture
async def envelope(tmp_path, monkeypatch):
    if sys.platform == "darwin":
        if os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") != "1":
            pytest.skip("actual Safehouse runs only in opted-in CI")
        assert not os.environ.get("CORSIA_THREAD_ID"), "never run Safehouse in an agent shell"
        assert shutil.which("safehouse"), "required Safehouse unavailable"
    elif sys.platform == "linux":
        if os.environ.get("CORSIA_REQUIRE_LINUX_FD_PROBE") != "1":
            pytest.skip("actual Linux chain requires opted-in CI")
        assert shutil.which("bwrap"), "required bubblewrap unavailable"
    else:
        pytest.skip("no supported envelope on this platform")
    from host_access.processes import ProcessOwner
    with tempfile.TemporaryDirectory(prefix="fdproof-", dir="/tmp") as raw:
        root = Path(raw).resolve()
        home = root / "home"
        workspace = home / "work"
        workspace.mkdir(parents=True)
        support = Path(__file__).with_name("inherited_fd_probe_support.py")
        (workspace / support.name).write_bytes(support.read_bytes())
        (workspace / "child.py").write_text(CHILD)
        (workspace / "sibling.py").write_text(SIBLING)
        owner = ProcessOwner(runtime_dir=root / "runtime", term_grace_seconds=0.2)
        terminals = {}
        async def transition(record, state):
            if state == "terminal":
                terminals.setdefault(record.handle, asyncio.Event()).set()
        owner.configure_transition_callback(transition)
        if sys.platform == "darwin":
            from tools import sandbox_config, shell_guard, unix_socket_policy
            monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
            monkeypatch.setattr(unix_socket_policy, "WORKSPACE", workspace)
            monkeypatch.setattr(unix_socket_policy.tempfile, "gettempdir", lambda: str(root))
        else:
            from tools.host_path_policy import ProtectedHostPathPolicy
            from tools.linux_sandbox import build_protected_argv
            state = home / ".local/state/corsia"
            state.mkdir(parents=True)
            policy = ProtectedHostPathPolicy(home, state)
        def argv_for(script, args):
            command = shlex.join(["/usr/bin/python3", str(workspace / script), *map(str, args)])
            if sys.platform == "darwin":
                profile = root / (script + ".sb")
                profile.write_text(shell_guard.execution_envelope_profile(
                    command, workspace, profile, leased=False, policies=[], thread_id=THREAD))
                return shell_guard.safehouse_argv(command, workspace, profile, policies=[], thread_id=THREAD), shell_guard.safehouse_subprocess_env(THREAD)
            env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
            return build_protected_argv(command, workspace, path_policy=policy,
                rw_roots=[workspace], ro_roots=[], readonly_overlays=[], env=env,
                thread_id=THREAD, executable=shutil.which("bwrap")), env
        try:
            yield owner, workspace, terminals, argv_for
        finally:
            await asyncio.wait_for(owner.shutdown(), timeout=10)


async def launch(box, script, args, background, fds=()):
    owner, workspace, terminals, argv_for = box
    argv, env = argv_for(script, args)
    result = await owner.spawn(argv, thread_id=THREAD, turn_id="probe-turn", cwd=workspace,
        env=env, timeout_seconds=12, background=background, inherited_fds=fds)
    if not background:
        return result.returncode, result.stdout, result.stderr
    event = terminals.setdefault(result.handle, asyncio.Event())
    record = owner.status(result.handle, thread_id=THREAD)
    if record.terminal_state == "running":
        await asyncio.wait_for(event.wait(), 15)
    record = owner.status(result.handle, thread_id=THREAD)
    return record.exit_code, record.log_path.read_bytes(), b""


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.parametrize("scenario", ["positive", "extra", "missing"])
async def test_actual_envelope_transport_and_sibling(envelope, background, scenario):
    peer, child = socket.socketpair()
    child_fd = child.detach()
    identity = os.fstat(child_fd)
    peer.setblocking(False)
    loop = asyncio.get_running_loop()
    data = []
    target = asyncio.create_task(launch(envelope, "child.py", [child_fd, scenario], background, (child_fd,)))
    sibling = asyncio.create_task(launch(envelope, "sibling.py",
        [child_fd, identity.st_dev, identity.st_ino], background))
    try:
        try:
            await loop.sock_sendall(peer, b"H")
            observed = await asyncio.wait_for(loop.sock_recv(peer, 1), 8)
        except (OSError, asyncio.TimeoutError) as exc:
            result = await target
            pytest.fail(f"descriptor unavailable/denied through generated envelope: {exc!r}, child={result!r}")
        if observed != b"I":
            result = await target
            pytest.fail(f"descriptor unavailable/denied through generated envelope: received {observed!r}, child={result!r}")
        sibling_code, sibling_out, sibling_err = await sibling
        assert sibling_code == 0, (sibling_code, sibling_out, sibling_err)
        assert b"ATTACHED" not in sibling_out, sibling_out
        assert not target.done(), "sibling check must run while intended endpoint is held"
        if scenario != "missing":
            for _ in range(2 if scenario == "extra" else 1):
                data.append(socket.socketpair())
            rights = array.array("i", [pair[0].fileno() for pair in data])
            peer.sendmsg([b"K"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
            for sent, _ in data:
                sent.close()
            if scenario == "positive":
                data[0][1].setblocking(False)
                assert await asyncio.wait_for(loop.sock_recv(data[0][1], 1), 5) == b"P"
            else:
                for _, remote in data:
                    remote.setblocking(False)
                    assert await asyncio.wait_for(loop.sock_recv(remote, 1), 5) == b""
                assert not target.done(), "rights closure must precede child exit"
        else:
            await loop.sock_sendall(peer, b"K")
        assert await asyncio.wait_for(loop.sock_recv(peer, 1), 8) == b""
        code, out, err = await target
        assert code == 0, (code, out, err)
    finally:
        peer.close()
        for left, right in data:
            left.close()
            right.close()
        for task in (target, sibling):
            if not task.done():
                task.cancel()
        await asyncio.gather(target, sibling, return_exceptions=True)
