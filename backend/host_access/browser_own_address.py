"""The serving CORSIA's own addresses, which the agents' browser never opens.

Anyone who signs in to CORSIA in the shared browser hands that sign-in to every agent driving it, so
the browser refuses the instance this backend belongs to. The serving instance is known by its ports,
not by its names: a name can be anything that reaches this machine (``localhost``, an address, a
host name, ``anything.localhost``, a public name that resolves to 127.0.0.1). So Chrome is launched with
``--host-resolver-rules`` that make every host at a serving port unresolvable, plus this machine's
Tailscale addresses at any port, and the saved outside address (any name the person reaches CORSIA by from
another device, such as a Tailscale name or their own domain) at its port only, 443 unless it names one.
Chrome applies these rules to every network request of every process: navigation, redirect, frame, popup,
fetch, XHR, WebSocket, worker.

CORSIA keeps its sign-in in the page origin's ``localStorage``, never in a cookie, so launch also clears
the stored data of each origin that serves its pages; origins are scheme, name and port, so no other port
is touched.

``serving_ports`` can scan every process on the machine, so async callers run these checks in a thread.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
from urllib.parse import urlsplit

import psutil

logger = logging.getLogger(__name__)
REFUSAL = "This address is the person's own CORSIA, and the agents' browser does not open it."
_WELL_KNOWN = frozenset({80, 443})  # a wildcard here would close the whole web; match machine names instead
_SCHEME_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale addresses
_SCAN_TTL = 30.0
_CLEAR_TIMEOUT = 5.0  # one origin
_CLEAR_BUDGET = 15.0  # all of them, so a browser that stops answering delays launch by this at most
_STORED_DATA = "local_storage,indexeddb,service_workers,cache_storage,file_systems"  # never cookies: per host
_frontend_scan: tuple[float, frozenset[int]] = (-_SCAN_TTL, frozenset())


def _listening(process: psutil.Process) -> set[int]:
    try:
        return {item.laddr.port for item in process.net_connections(kind="tcp") if item.status == psutil.CONN_LISTEN}
    except (psutil.Error, OSError):
        return set()


def _page_port() -> int:
    """The port this backend serves its pages and API on, from the same source that binds it."""
    from infra.default_port import DEFAULT_BACKEND_PORT
    configured = os.environ.get("BACKEND_PORT", "")
    return int(configured) if configured.isdigit() else DEFAULT_BACKEND_PORT


def _backend_ports() -> set[int]:
    """This backend's own port and its measure receiver's candidates, from the same sources that bind them.

    Not every socket the process holds: a page server a test or tool starts in-process is someone else's site.
    """
    from infra.otlp_listener import _ports as receiver_ports
    return {_page_port(), *receiver_ports()}


def _frontend_ports(backend_ports: set[int]) -> frozenset[int]:
    """Ports of a separate frontend dev server in front of this backend (its environment names the backend)."""
    global _frontend_scan
    stamp, found = _frontend_scan
    if time.monotonic() - stamp < _SCAN_TTL:
        return found
    ports: set[int] = set()
    for process in psutil.process_iter():
        try:
            if urlsplit(process.environ().get("CORSIA_BACKEND_URL", "")).port in backend_ports:
                ports |= _listening(process)
        except (psutil.Error, OSError, ValueError):
            continue
    _frontend_scan = (time.monotonic(), frozenset(ports))
    return _frontend_scan[1]


def serving_ports() -> frozenset[int]:
    """The ports of the serving instance: backend listeners, the approved devices range, a frontend dev server."""
    from infra.devices_listener import DEFAULT_PORT, FALLBACK_PORTS
    backend = _backend_ports()
    return frozenset(backend | {DEFAULT_PORT, *FALLBACK_PORTS} | _frontend_ports(backend))


def tailnet_addresses() -> set[str]:
    """This machine's Tailscale addresses, matched at any port."""
    hosts: set[str] = set()
    for entries in psutil.net_if_addrs().values():
        hosts |= {item.address for item in entries
                  if item.family == socket.AF_INET and ipaddress.ip_address(item.address) in _CGNAT}
    return hosts


def outside_endpoint() -> tuple[str, int] | None:
    """The saved outside address as a name and port (443 unless it names one), or None when none is saved."""
    from core.devices_runtime import runtime
    address = re.sub(r"^https?://", "", runtime.outside_address or "", flags=re.IGNORECASE).removesuffix("/")
    host, _, port = address.partition(":")
    host = host.rstrip(".").lower()
    if not host:
        return None
    return host, int(port) if port.isdigit() else 443


def _machine_hosts() -> set[str]:
    """Names for this machine, matched only at ports a wildcard would over-block."""
    hostname = socket.gethostname().rstrip(".").lower()
    hosts = {*_LOOPBACK_NAMES, hostname, f"{hostname.removesuffix('.local')}.local"}
    for entries in psutil.net_if_addrs().values():
        hosts |= {item.address for item in entries if item.family == socket.AF_INET}
    return hosts


def resolver_rules() -> str:
    """The Chrome ``--host-resolver-rules`` value that makes the serving instance unresolvable."""
    ports = serving_ports()
    rules = [f"MAP *:{port} ~NOTFOUND" for port in sorted(ports - _WELL_KNOWN)]
    rules += [f"MAP {host}:{port} ~NOTFOUND" for port in sorted(ports & _WELL_KNOWN)
              for host in [*sorted(_machine_hosts()), "*.localhost"]]
    rules += [f"MAP {host} ~NOTFOUND" for host in sorted(tailnet_addresses())]
    if (outside := outside_endpoint()) is not None:
        rules.append(f"MAP {outside[0]}:{outside[1]} ~NOTFOUND")
    return ", ".join(rules)


def is_own_address(url: str) -> bool:
    """Whether a web address is the serving instance, by the same rule the browser enforces."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port or _SCHEME_PORT.get(parts.scheme.lower())
    except ValueError:
        return False
    host = f"[{host}]" if ":" in host else host  # an IPv6 literal, spelled as the machine names list it
    if host in tailnet_addresses() or (host, port) == outside_endpoint():
        return True
    ports = serving_ports()
    if port in ports - _WELL_KNOWN:
        return True
    return port in ports and (host in _machine_hosts() or host.endswith(".localhost"))


def _serving_origins() -> list[str]:
    """Every scheme, name and port the serving instance's pages could have stored data under.

    Only ports that serve pages: the measure receiver's candidates take logs and never hold a sign-in.
    """
    ports = {_page_port()} | _frontend_ports(_backend_ports())
    tailnet = tailnet_addresses()
    origins = [f"{scheme}://{host}:{port}" for scheme in ("http", "https")
               for host in _machine_hosts() | tailnet for port in ports]
    origins += [f"https://{host}" for host in tailnet]
    if (outside := outside_endpoint()) is not None:
        host, port = outside
        origins.append(f"https://{host}" if port == 443 else f"https://{host}:{port}")
    return origins


async def _first_page(endpoint: str) -> str:
    """The launch page's own endpoint; Chrome answers storage commands on a page, not on the browser itself."""
    from host_access.browser_cdp import _page_endpoint
    from host_access.browser_session_state import BrowserSessionError
    for _ in range(20):
        try:
            return await asyncio.to_thread(_page_endpoint, endpoint)
        except BrowserSessionError:
            await asyncio.sleep(0.1)
    raise BrowserSessionError("the browser opened no page")


async def clear_serving_storage(endpoint: str) -> None:
    """At launch, drop whatever the saved profile holds for each serving origin, notably the CORSIA sign-in."""
    from host_access.browser_cdp import CdpClient
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CLEAR_BUDGET
    try:
        async with CdpClient(await _first_page(endpoint)) as cdp:
            origins = await asyncio.to_thread(_serving_origins)
            for index, origin in enumerate(origins):
                if (left := deadline - loop.time()) <= 0:
                    logger.warning("stored data of %d serving origins was not cleared: the browser stopped answering "
                                   "within %.0f s", len(origins) - index, _CLEAR_BUDGET)
                    return
                try:
                    await asyncio.wait_for(cdp.call("Storage.clearDataForOrigin", origin=origin,
                                                    storageTypes=_STORED_DATA), min(_CLEAR_TIMEOUT, left))
                except Exception:
                    logger.warning("stored data of %s could not be cleared from the browser profile", origin,
                                   exc_info=True)
    except Exception:
        logger.warning("stored data of the serving CORSIA could not be cleared from the browser profile", exc_info=True)
