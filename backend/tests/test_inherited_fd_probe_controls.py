"""Local probe-sensitivity controls; no generated sandbox envelope is exercised."""
from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from tests.test_inherited_fd_envelope import SIBLING

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("inherit", [False, True])
def test_sibling_detects_possession_after_intended_child_consumes_hello(inherit):
    """A leaked endpoint must be detected independently of greeting consumption."""
    import os

    peer, child = socket.socketpair()
    try:
        identity = os.fstat(child.fileno())
        peer.sendall(b"H")
        assert child.recv(1) == b"H"
        child.sendall(b"I")
        assert peer.recv(1) == b"I"
        result = subprocess.run(
            [sys.executable, "-B", "-c", SIBLING, str(child.fileno()),
             str(identity.st_dev), str(identity.st_ino)],
            pass_fds=(child.fileno(),) if inherit else (),
            capture_output=True, timeout=3, check=False,
        )
        assert result.returncode == (86 if inherit else 0), result
        assert result.stdout == (b"ATTACHED\n" if inherit else b""), result
        if inherit:
            peer.settimeout(1)
            assert peer.recv(1) == b"S"
    finally:
        peer.close()
        child.close()


def test_sibling_does_not_misidentify_an_unrelated_socket_at_same_number():
    """Identity prevents writes to an unrelated socket that reused the FD slot."""
    import os

    original, original_peer = socket.socketpair()
    replacement, replacement_peer = socket.socketpair()
    slot = original.fileno()
    identity = os.fstat(slot)
    original.close()
    os.dup2(replacement.fileno(), slot, inheritable=False)
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", SIBLING, str(slot),
             str(identity.st_dev), str(identity.st_ino)],
            pass_fds=(slot,), capture_output=True, timeout=3, check=False,
        )
        assert result.returncode == 0, result
        assert result.stdout == b"", result
    finally:
        os.close(slot)
        for sock in (original_peer, replacement, replacement_peer):
            sock.close()
