"""Invented-byte receiver used only by the inherited-FD transport probes."""
from __future__ import annotations

import array
import os
import socket


def receive_one(control):
    """Receive exactly one connected stream right or close rights and poison control."""
    itemsize = array.array("i").itemsize
    received = []
    try:
        status, ancillary, flags, _ = control.recvmsg(1, socket.CMSG_SPACE(2 * itemsize))
        malformed = bool(flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC))
        for level, kind, payload in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                malformed = True
                continue
            if len(payload) % itemsize:
                malformed = True
            values = array.array("i")
            values.frombytes(payload[:len(payload) - len(payload) % itemsize])
            received.extend(values)
        for fd in received:
            os.set_inheritable(fd, False)
        if malformed or status != b"K" or len(received) != 1:
            raise ValueError("malformed descriptor reply")
        sock = socket.socket(fileno=received[0])
        received.clear()
        try:
            if sock.family != socket.AF_UNIX or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                raise ValueError("not a Unix stream socket")
            sock.getpeername()
            return sock
        except BaseException:
            sock.close()
            raise
    except BaseException:
        for fd in received:
            os.close(fd)
        control.close()
        raise
