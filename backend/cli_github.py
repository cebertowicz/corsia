#!/usr/bin/env python3
"""Read GitHub through a script run's broker channel."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys

SOCKET_ENV = "CORSIA_SCRIPT_GITHUB_SOCKET"


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise OSError("broker closed the connection")
        chunks.extend(part)
    return bytes(chunks)


def read(args: list[str]) -> int:
    path = os.environ.get(SOCKET_ENV)
    if not path:
        print("GitHub reads are available only to a script run that a rule started from an event "
              "naming a repository", file=sys.stderr)
        return 1
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60)
            client.connect(path)
            client.sendall(json.dumps(args).encode() + b"\n")
            size = int.from_bytes(_read_exact(client, 8), "big")
            response = json.loads(_read_exact(client, size))
        stdout, stderr, code = response["stdout"], response["stderr"], int(response["code"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"GitHub read channel is unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    if sys.argv[1:3] == ["github", "read"]:
        sys.exit(read(sys.argv[3:]))
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        command = shutil.which("corsia", path=directory)
        if command and os.path.realpath(command) != os.path.realpath(__file__):
            os.execv(command, [command, *sys.argv[1:]])
    print("Usage: corsia github read <api-path>", file=sys.stderr)
    sys.exit(2)
