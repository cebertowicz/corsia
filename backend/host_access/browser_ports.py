"""Static network boundary shared by managed browser launch and agent profiles."""
from __future__ import annotations

import os
import socket
from pathlib import Path

# Two listeners per session: Chrome CDP and agent-browser's stream server.
# Chrome refuses page traffic (including WebSockets) to these restricted ports;
# native CDP and stream clients can connect. These >=1024 ports bind unprivileged.
# Source: Chromium net/base/port_util.cc, kRestrictedPorts (154.0.8037.58):
# https://chromium.googlesource.com/chromium/src/+/154.0.8037.58/net/base/port_util.cc
# 17 ports permit eight concurrent sessions when free; reserve_pair skips busy ports.
# The same fixed inventory protects shells that predate a browser session.
RESERVED_PORTS = (
    1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566,
    6665, 6666, 6667, 6668, 6669, 6697, 10080,
)
SOCKET_ROOT = Path(f"/tmp/corsia-ab-{os.getuid()}")


def reserve_pair() -> tuple[socket.socket, socket.socket]:
    """Hold two distinct unused loopback listeners until their producers start."""
    held: list[socket.socket] = []
    try:
        for port in RESERVED_PORTS:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                listener.close()
                continue
            held.append(listener)
            if len(held) == 2:
                return held[0], held[1]
        raise RuntimeError("managed browser listener ports are unavailable; close an unused browser and retry")
    except BaseException:
        for listener in held:
            listener.close()
        raise


def sandbox_rules() -> str:
    """Keep browser TCP and daemon sockets closed even in leased agent profiles."""
    rules = [";; CORSIA MANAGED BROWSER BOUNDARY"]
    for port in RESERVED_PORTS:
        rules.extend((
            f'(deny network-outbound (remote tcp "*:{port}"))',
            f'(deny network-bind (local tcp "*:{port}"))',
        ))
    for root in dict.fromkeys((SOCKET_ROOT, SOCKET_ROOT.resolve())):
        rules.extend((
            f'(deny file-read* file-write* (subpath "{root}"))',
            f'(deny network-outbound (remote unix-socket (subpath "{root}")))',
            f'(deny network-bind (local unix-socket (subpath "{root}")))',
        ))
    return "\n".join(rules)
