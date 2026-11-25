"""Whether a site is CORSIA's own address, for which no login is ever saved (C8).

A saved CORSIA sign-in in the agents' browser would let an agent pass the
person's own controls, so a login for CORSIA itself is refused. CORSIA's own
address is any name or address of this computer on a port CORSIA serves, and
the outside address set for reaching it from other devices.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sqlite3
from urllib.parse import urlsplit

import psutil

from infra.default_port import DEFAULT_BACKEND_PORT
from infra.devices_listener import DEFAULT_PORT as DEVICES_PORT, FALLBACK_PORTS as DEVICES_FALLBACK_PORTS
from infra.devices_names import local_hostname
from infra.saved_logins import normalized_site
from infra.settings import get_devices_outside_address


def _serving_ports() -> frozenset[int]:
    """Every port a CORSIA on this computer serves on.

    The default port and the ``BACKEND_PORT`` override (the main listener, as
    ``cli_sources`` and the harness bridge read it), and every port the devices
    listener may bind (its default and fallbacks). Any CORSIA on this computer
    counts, not only this process.
    """
    ports = {DEFAULT_BACKEND_PORT, DEVICES_PORT, *DEVICES_FALLBACK_PORTS}
    override = os.environ.get("BACKEND_PORT", "")
    if override.isdigit():
        ports.add(int(override))
    return frozenset(ports)


def _is_local_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return any(candidate.is_loopback or candidate.is_unspecified for candidate in (address, mapped) if candidate)


def _this_computer_hosts() -> frozenset[str]:
    """This computer's names and interface addresses, lowercase, IPv6 without brackets or zone."""
    hosts = {"localhost", local_hostname(), socket.gethostname().rstrip(".").lower()}
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family in {socket.AF_INET, socket.AF_INET6}:
                try:
                    hosts.add(ipaddress.ip_address(entry.address.split("%", 1)[0]).compressed)
                except ValueError:
                    continue
    return frozenset(hosts)


def _outside_site(db: sqlite3.Connection | None) -> str | None:
    """The outside address as a normalized site, or None when none is set.

    The devices settings keep the name as typed, which may end in a dot
    (``home.example.com.``); the browser reports the host without it, so the dot
    is dropped before comparing. A stored value that still isn't a site raises
    ``KeyStoreError``, which refuses the offer rather than skipping this check.
    """
    address = get_devices_outside_address(db=db)
    if not address:
        return None
    host, colon, port = address.partition(":")
    return normalized_site(f"https://{host.removesuffix('.')}{colon}{port}")


def is_own_site(site: str, *, db: sqlite3.Connection | None = None) -> bool:
    """Whether ``site``, already normalized by ``normalized_site``, is CORSIA's own address."""
    if site == _outside_site(db):
        return True
    parts = urlsplit(site)
    host = (parts.hostname or "").rstrip(".")
    if (parts.port or 443) not in _serving_ports():
        return False
    return host.endswith(".localhost") or _is_local_ip(host) or host in _this_computer_hosts()
