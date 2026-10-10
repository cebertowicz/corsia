"""Whether a request is a direct local connection, never a forwarded one.

A private-network forwarder (Tailscale serve, a reverse proxy) hands every
remote request to CORSIA from 127.0.0.1, so the peer address alone cannot show
the caller is on this computer. A request that carries a forwarding header
was relayed and never earns local trust. Every place that grants something
because a connection is local calls :func:`is_direct_loopback`.
"""

from __future__ import annotations

from ipaddress import ip_address

from starlette.requests import HTTPConnection

FORWARDING_HEADERS = ("tailscale-user-login", "x-forwarded-for", "forwarded", "x-real-ip")


def is_forwarded(connection: HTTPConnection) -> bool:
    """True when the request names a forwarder in any forwarding header."""
    return any(name in connection.headers for name in FORWARDING_HEADERS)


def is_direct_loopback(connection: HTTPConnection) -> bool:
    """True only for a loopback peer whose request was not forwarded."""
    if is_forwarded(connection) or connection.client is None:
        return False
    try:
        return ip_address(connection.client.host).is_loopback
    except ValueError:
        return False
