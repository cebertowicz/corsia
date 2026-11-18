"""Local descriptor ownership checks; these do not prove a sandbox envelope."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import sys

import pytest

from host_access.process_launch import gated_launch_spec, prepare_spawn
from host_access.processes import ProcessOwner, ProcessOwnerError

pytestmark = pytest.mark.integration


@pytest.fixture
async def owner(tmp_path):
    owner = ProcessOwner(runtime_dir=tmp_path / "runtime", term_grace_seconds=0.1)
    async def transition(_record, _state):
        pass
    owner.configure_transition_callback(transition)
    yield owner
    await owner.shutdown()


def _arguments(tmp_path, **changes):
    values = dict(thread_id="fd-test", turn_id="turn", cwd=tmp_path,
                  env=os.environ, timeout_seconds=5, background=False)
    values.update(changes)
    return values


def _assert_closed(fd):
    with pytest.raises(OSError):
        os.fstat(fd)


def test_prepare_merges_transport_cwd_and_gate(tmp_path):
    with socket.socketpair()[0] as sock:
        cwd_fd = os.open(tmp_path, os.O_RDONLY)
        try:
            assert not os.get_inheritable(sock.fileno())
            spec = prepare_spawn([sys.executable], cwd=tmp_path, cwd_fd=cwd_fd,
                                 inherited_fds=(sock.fileno(),), cleanup_paths=(),
                                 error_type=ProcessOwnerError)
            assert spec.options["pass_fds"] == (sock.fileno(), cwd_fd)
            _, options, read_fd, write_fd = gated_launch_spec(spec.argv, spec.options)
            try:
                assert options["pass_fds"] == (sock.fileno(), cwd_fd, read_fd)
            finally:
                os.close(read_fd)
                os.close(write_fd)
        finally:
            os.close(cwd_fd)


@pytest.mark.asyncio
async def test_foreground_closes_parent_before_child_completion(owner, tmp_path):
    peer, child = socket.socketpair()
    fd = child.detach()
    assert not os.get_inheritable(fd)
    script = "import os,sys; f=int(sys.argv[1]); os.write(f,b'X'); os.read(f,1)"
    task = asyncio.create_task(owner.spawn([sys.executable, "-c", script, str(fd)],
                              inherited_fds=(fd,), **_arguments(tmp_path)))
    peer.setblocking(False)
    try:
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 1), 3) == b"X"
        _assert_closed(fd)
        assert not task.done()
        replacement = os.open(os.devnull, os.O_RDONLY)
        if replacement != fd:
            os.dup2(replacement, fd)
            os.close(replacement)
        try:
            await asyncio.get_running_loop().sock_sendall(peer, b"Y")
            assert (await task).returncode == 0
            os.fstat(fd)  # final cleanup did not close a reused descriptor number
        finally:
            os.close(fd)
    finally:
        peer.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_closes_parent_while_child_runs(owner, tmp_path):
    peer, child = socket.socketpair()
    fd = child.detach()
    original = os.fstat(fd)
    script = "import os,sys; f=int(sys.argv[1]); os.write(f,b'X'); os.read(f,1)"
    try:
        result = await owner.spawn([sys.executable, "-c", script, str(fd)],
            inherited_fds=(fd,), **_arguments(tmp_path, background=True))
        peer.setblocking(False)
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(peer, 1), 3) == b"X"
        try:
            current = os.fstat(fd)
        except OSError:
            pass
        else:
            assert (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
        assert owner.status(result.handle, thread_id="fd-test").terminal_state == "running"
        await asyncio.get_running_loop().sock_sendall(peer, b"Y")
    finally:
        peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["duplicate", "inheritable"])
async def test_invalid_transferred_descriptors_are_closed(owner, tmp_path, invalid):
    peer, child = socket.socketpair()
    fd = child.detach()
    if invalid == "inheritable":
        os.set_inheritable(fd, True)
    descriptors = (fd, fd) if invalid == "duplicate" else (fd,)
    try:
        with pytest.raises(ProcessOwnerError):
            await owner.spawn([sys.executable, "-c", "pass"],
                              inherited_fds=descriptors, **_arguments(tmp_path))
        _assert_closed(fd)
    finally:
        peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["validation", "admission", "spawn", "cancel"])
async def test_transferred_fd_closed_on_early_failure(owner, tmp_path, monkeypatch, failure):
    peer, child = socket.socketpair()
    fd = child.detach()
    kwargs = _arguments(tmp_path)
    try:
        if failure == "validation":
            kwargs["thread_id"] = ""
        elif failure == "admission":
            async def refuse(**_kwargs):
                raise ProcessOwnerError("refused")
            monkeypatch.setattr(owner, "_require_process_admission", refuse)
        elif failure == "spawn":
            async def fail(*_args, **_kwargs):
                raise OSError("injected spawn failure")
            monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
        else:
            await owner._owner_lock.acquire()
        task = asyncio.create_task(owner.spawn([sys.executable, "-c", "pass"],
                                  inherited_fds=(fd,), **kwargs))
        if failure == "cancel":
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            owner._owner_lock.release()
        else:
            with pytest.raises((ProcessOwnerError, OSError)):
                await task
        _assert_closed(fd)
    finally:
        if failure == "cancel" and owner._owner_lock.locked():
            owner._owner_lock.release()
        peer.close()
