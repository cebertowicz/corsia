#!/usr/bin/env python3
"""Script client for owner tools, saved items and the GitHub CLI."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sys
from urllib.parse import urlsplit

SOCKET_ENV = "CORSIA_SCRIPT_TOOL_SOCKET"
SEND_SOCKET_ENV = "CORSIA_SCRIPT_SEND_SOCKET"
GIVEN_SOCKET_ENV = "CORSIA_SCRIPT_GIVEN_SOCKET"
SEND_USAGE = ("Usage: corsia send <METHOD> <https-url> [-H 'Name: value']... [--data <text>|@<file>|-]\n"
              "Name a saved item with {{corsia-item:<name>}} or a saved login's password with "
              "{{corsia-login:<id>}} in a header value or a JSON body; CORSIA puts the value in and sends it.")
ITEM_USAGE = "Usage: corsia item <name>  (prints a saved item this script's approval gives to the script)"


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


def _send_options(args: list[str]) -> tuple[list[tuple[str, str]], bytes | None]:
    headers: list[tuple[str, str]] = []
    data: bytes | None = None
    rest = iter(args)
    for flag in rest:
        value = next(rest, None)
        if value is None or flag not in ("-H", "--data"):
            raise ValueError(flag)
        if flag == "-H":
            name, sep, header = value.partition(":")
            if not sep or not name.strip():
                raise ValueError(flag)
            headers.append((name.strip(), header.strip()))
        elif value == "-":
            data = sys.stdin.buffer.read()
        elif value.startswith("@"):
            with open(value[1:], "rb") as handle:
                data = handle.read()
        else:
            data = value.encode("utf-8")
    return headers, data


def send(args: list[str]) -> int:
    """Ask the run's forwarder to send one request; the answer's body goes to stdout."""
    path = os.environ.get(SEND_SOCKET_ENV)
    try:
        method, url = args[0].upper(), args[1]
        headers, data = _send_options(args[2:])
        host = urlsplit(url).hostname or ""
    except (IndexError, ValueError):
        print(SEND_USAGE, file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Couldn't read the --data file: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not path:
        print("This script run has no saved item to send: its approval names none with a place, or they "
              "were withheld from this run.", file=sys.stderr)
        return 1
    try:
        conn = http.client.HTTPConnection(host)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect(path)
        conn.request(method, url, body=data, headers=dict(headers))
        response = conn.getresponse()
        body = response.read()
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        print(f"Saved-item forwarder unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(body)
    if response.status >= 400:
        print(f"HTTP {response.status}", file=sys.stderr)
        return 1
    return 0


def item(args: list[str]) -> int:
    """Print one saved item this run's approval gives to the script, exactly, with no newline."""
    if len(args) != 1:
        print(ITEM_USAGE, file=sys.stderr)
        return 2
    path = os.environ.get(GIVEN_SOCKET_ENV)
    if not path:
        print("This script run has no saved item given to it: its approval gives none to the script, or "
              "they were withheld from this run.", file=sys.stderr)
        return 1
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            client.sendall(json.dumps({"name": args[0]}).encode() + b"\n")
            size = int.from_bytes(_read_exact(client, 8), "big")
            answer = json.loads(_read_exact(client, size))
    except (OSError, ValueError) as exc:
        print(f"Saved-item socket unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(answer, dict) or not isinstance(answer.get("value"), str):
        error = answer.get("error") if isinstance(answer, dict) else None
        print(error if isinstance(error, str) else "The saved item couldn't be read.", file=sys.stderr)
        return 1
    sys.stdout.write(answer["value"])
    return 0


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
    """The first ``gh`` on PATH that this run can open.

    A run's sandbox may list a folder on PATH yet refuse to open what it holds (on macOS,
    ~/.local/bin), so a ``gh`` there fails instead of running; the next one is tried.
    """
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and (found := shutil.which("gh", path=directory)):
            try:
                os.close(os.open(found, os.O_RDONLY))
            except OSError:
                continue
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
    if args and args[0] == "send":
        return send(args[1:])
    if args and args[0] == "item":
        return item(args[1:])
    if args and args[0] == "github":
        gh_args = args[1:]
        if gh_args[:1] == ["read"]:
            gh_args = ["api", *gh_args[1:]]
        gh = _gh()
        if gh is None:
            print("No GitHub CLI (gh) on PATH that this script run can open; install gh in a "
                  "system folder such as /usr/local/bin", file=sys.stderr)
            return 127
        os.execv(gh, [gh, *gh_args])
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        command = shutil.which("corsia", path=directory)
        if command and os.path.realpath(command) != os.path.realpath(__file__):
            os.execv(command, [command, *args])
    print("Usage: corsia tool <name> '<json-object>' | corsia send <METHOD> <https-url> ... | "
          "corsia item <name> | corsia github <args>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
