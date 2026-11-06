#!/usr/bin/env python3
"""Script client for owner tools and the GitHub CLI."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys

SOCKET_ENV = "CORSIA_SCRIPT_TOOL_SOCKET"


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise OSError("tool channel closed the connection")
        chunks.extend(part)
    return bytes(chunks)


def tool(args: list[str]) -> int:
    if len(args) != 2:
        print("Usage: corsia tool <name> '<json-object>'", file=sys.stderr)
        return 2
    path = os.environ.get(SOCKET_ENV)
    if not path:
        print("Authenticated tools are unavailable to this script.", file=sys.stderr)
        return 1
    try:
        params = json.loads(args[1])
        if not isinstance(params, dict):
            raise ValueError("tool arguments must be a JSON object")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            client.sendall(json.dumps({"name": args[0], "arguments": params}).encode() + b"\n")
            size = int.from_bytes(_read_exact(client, 8), "big")
            response = json.loads(_read_exact(client, size))
        sys.stdout.write(response["stdout"])
        sys.stderr.write(response["stderr"])
        return int(response["code"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Tool channel unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1


def legacy_github_read(args: list[str], path: str) -> int:
    """Use the existing repository-scoped broker for rule-started scripts."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60)
            client.connect(path)
            client.sendall(json.dumps(args).encode() + b"\n")
            size = int.from_bytes(_read_exact(client, 8), "big")
            response = json.loads(_read_exact(client, size))
        sys.stdout.write(response["stdout"])
        sys.stderr.write(response["stderr"])
        return int(response["code"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"GitHub read channel is unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1


def _gh() -> str | None:
    """The first ``gh`` on PATH outside ~/.local/bin.

    The run's sandbox refuses to open what that folder holds, so a ``gh`` linked there
    fails instead of running; CORSIA never resolves a CLI from it (``cli_location``).
    """
    skipped = os.path.realpath(os.path.expanduser("~/.local/bin"))
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and os.path.realpath(directory) != skipped:
            if found := shutil.which("gh", path=directory):
                return found
    return None


def main(args: list[str]) -> int:
    legacy_socket = os.environ.get("CORSIA_SCRIPT_GITHUB_SOCKET")
    if args[:2] == ["github", "read"]:
        if legacy_socket:
            return legacy_github_read(args[2:], legacy_socket)
        if not os.environ.get(SOCKET_ENV):
            print("GitHub reads are available only to a script run that a rule started from an event "
                  "naming a repository", file=sys.stderr)
            return 1
    if args and args[0] == "tool":
        return tool(args[1:])
    if args and args[0] == "github":
        gh_args = args[1:]
        if gh_args[:1] == ["read"]:
            gh_args = ["api", *gh_args[1:]]
        gh = _gh()
        if gh is None:
            print("GitHub CLI (gh) is not installed where a script run can reach it", file=sys.stderr)
            return 127
        os.execv(gh, [gh, *gh_args])
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        command = shutil.which("corsia", path=directory)
        if command and os.path.realpath(command) != os.path.realpath(__file__):
            os.execv(command, [command, *args])
    print("Usage: corsia tool <name> '<json-object>' | corsia github <args>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
