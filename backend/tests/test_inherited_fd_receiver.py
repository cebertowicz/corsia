"""Local tests of the proof-only strict ancillary receiver."""
import array
import os
import socket

import pytest

from tests.inherited_fd_probe_support import receive_one

pytestmark = pytest.mark.integration


def test_actual_right_roundtrip():
    control, remote = socket.socketpair()
    sent, peer = socket.socketpair()
    try:
        remote.sendmsg([b"K"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [sent.fileno()]))])
        received = receive_one(control)
        try:
            assert not os.get_inheritable(received.fileno())
            received.sendall(b"P")
            assert peer.recv(1) == b"P"
        finally:
            received.close()
    finally:
        control.close()
        remote.close()
        sent.close()
        peer.close()


def test_rejects_seqpacket_type_reported_by_socket_adapter(monkeypatch):
    """Simulate type metadata: macOS cannot create AF_UNIX/SOCK_SEQPACKET."""
    from types import SimpleNamespace
    from tests import inherited_fd_probe_support as support

    class SeqpacketMetadata(socket.socket):
        @property
        def type(self):
            return socket.SOCK_SEQPACKET

        def getsockopt(self, level, option, *args):
            if (level, option) == (socket.SOL_SOCKET, socket.SO_TYPE):
                return socket.SOCK_SEQPACKET
            return super().getsockopt(level, option, *args)

    control, remote = socket.socketpair()
    sent, peer = socket.socketpair()
    adapter = SimpleNamespace(**vars(socket))
    adapter.socket = SeqpacketMetadata
    monkeypatch.setattr(support, "socket", adapter)
    received = None
    try:
        remote.sendmsg([b"K"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                               array.array("i", [sent.fileno()]))])
        sent.close()
        with pytest.raises(ValueError, match="Unix stream"):
            received = receive_one(control)
        assert control.fileno() == -1
        peer.settimeout(1)
        assert peer.recv(1) == b""
    finally:
        if received is not None:
            received.close()
        for sock in (control, remote, sent, peer):
            sock.close()


@pytest.mark.parametrize("case", ["extra", "missing", "status", "truncated", "partial", "unknown", "not-socket", "unconnected"])
def test_rejection_closes_all_received_rights_and_control(case):
    sent, peer = socket.socketpair()
    if case == "not-socket":
        first = os.open(os.devnull, os.O_RDONLY)
    elif case == "unconnected":
        first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).detach()
    else:
        first = os.dup(sent.fileno())
    second = os.dup(sent.fileno()) if case == "extra" else None
    fds = [first] + ([second] if second is not None else [])
    payload = array.array("i", fds).tobytes()
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, payload)]
    if case == "missing":
        os.close(first)
        fds = []
        ancillary = []
    if case == "partial":
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, payload + b"x")]
    if case == "unknown":
        ancillary.append((socket.SOL_SOCKET, -1, b"x"))

    class Reply:
        closed = False
        def recvmsg(self, *_args):
            return b"!" if case == "status" else b"K", ancillary, socket.MSG_CTRUNC if case == "truncated" else 0, None
        def close(self):
            self.closed = True

    control = Reply()
    try:
        with pytest.raises((ValueError, OSError)):
            receive_one(control)
        assert control.closed
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        sent.close()
        peer.close()
