"""Dedicated TLS listener for pairing and remote CORSIA devices."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs

import psutil
import uvicorn
from starlette.responses import JSONResponse

from infra.auth import authenticate, is_person_session, token_from_request
from infra.db_async import run_in_db
# Preserve listener imports while identity storage, cache, and signing have one owner.
from infra.devices_identity import (
    _key_paths as _key_paths,
    _parsed_files as _parsed_files,
    ensure_host_key as ensure_host_key,
    host_spki_der as host_spki_der,
    host_spki_sha256 as host_spki_sha256,
    serialization as serialization,
    sign_identity_proof as sign_identity_proof,
    x509 as x509,
)
from infra.devices_server import DevicesServer
from infra.dns_sd import DNSServiceAnnouncement


DEFAULT_PORT = 47901
FALLBACK_PORTS = range(47902, 47911)
_PRIVATE_V4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_PEER_NETWORKS = (*_PRIVATE_V4_NETWORKS, ipaddress.ip_network("fc00::/7"))
_EXCLUDED_INTERFACE_PREFIXES = ("lo", "utun", "tailscale", "tun", "tap", "ppp", "vpn", "bridge")
logger = logging.getLogger(__name__)


def lan_addresses(*, include_link_local: bool, interface_addrs=None) -> list[str]:
    """Return only addresses reachable through the current devices listener."""
    interface_addrs = psutil.net_if_addrs() if interface_addrs is None else interface_addrs
    addresses: list[tuple[int, int, str]] = []
    for interface, entries in interface_addrs.items():
        if interface.lower().startswith(_EXCLUDED_INTERFACE_PREFIXES):
            continue
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            value = entry.address
            try:
                address = ipaddress.ip_address(value.split("%", 1)[0])
            except ValueError:
                continue
            if address.is_loopback:
                continue
            if address.version == 4:
                include = any(address in network for network in _PRIVATE_V4_NETWORKS)
            elif address.is_link_local:
                include = include_link_local
            else:
                include = address in ipaddress.ip_network("fc00::/7")
            if include:
                addresses.append((address.version, int(address), value))
    return [value for _, _, value in sorted(set(addresses))]


_PUBLIC_API_ROUTES = frozenset({("GET", "/api/health"), ("GET", "/api/version"), ("GET", "/api/auth/status"), ("POST", "/api/auth/login"), ("POST", "/api/devices/pair/start"), ("POST", "/api/devices/pair/finish"), ("GET", "/api/devices/outside-address"), ("POST", "/api/devices/identity")})
_LOCAL_ONLY_POST_ROUTES = frozenset({("POST", "/api/devices/pair/start"), ("POST", "/api/devices/pair/finish")})
_FORWARDING_HEADERS = frozenset({b"forwarded", b"x-forwarded-for", b"x-real-ip"})


class DevicesApp:
    """Tag Devices scopes and limit their HTTP access to pairing-safe routes."""

    def __init__(self, app, *, outside_enabled: Callable[[], bool] | None = None):
        self._app = app
        self._outside_enabled = outside_enabled or (lambda: False)
        self.stopping = False

    @staticmethod
    def _handshake_token(scope) -> tuple[str | None, bool]:
        """The token the API routes read, and whether the request names more than one.

        The gate and the routes disagree about which duplicate wins (Starlette keeps the last query value and the
        first header), so a request with two is refused instead of guessed at.
        """
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True).get("token", [])
        headers = [value.decode("latin-1") for name, value in scope.get("headers", ()) if name == b"authorization"]
        bearer = token_from_request(None, headers[0]) if headers else None
        ambiguous = len(query) > 1 or len(headers) > 1 or bool(query and query[0] and bearer and query[0] != bearer)
        return token_from_request(query[0] if query else None, headers[0] if headers else None), ambiguous

    @classmethod
    async def _has_human_session(cls, scope) -> bool:
        token, ambiguous = cls._handshake_token(scope)
        if ambiguous:
            return False
        user = await run_in_db(authenticate, token)
        return user is not None and is_person_session(user)

    @staticmethod
    def _may_peer_reach_devices(scope) -> bool:
        """Return whether a peer may reach the devices listener."""
        try:
            peer = ipaddress.ip_address(str(scope["client"][0]).split("%", 1)[0])
        except (KeyError, TypeError, IndexError, ValueError):
            return False
        if peer.version == 6 and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        return peer.is_loopback or peer.is_link_local or any(peer in network for network in _PEER_NETWORKS)

    @staticmethod
    def _peer_is_on_this_network(scope) -> bool:
        """The pairing/sign-in network: private, ULA, or link-local, never loopback."""
        try:
            peer = ipaddress.ip_address(str(scope["client"][0]).split("%", 1)[0])
        except (KeyError, TypeError, IndexError, ValueError):
            return False
        if peer.version == 6 and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        return not peer.is_loopback and (peer.is_link_local or any(peer in network for network in _PEER_NETWORKS))

    @classmethod
    def _requires_same_network(cls, scope) -> bool:
        if (scope.get("method"), scope.get("path")) not in _LOCAL_ONLY_POST_ROUTES:
            return False
        headers = {name.lower() for name, _ in scope.get("headers", ())}
        return bool(headers & _FORWARDING_HEADERS) or not cls._peer_is_on_this_network(scope)

    async def __call__(self, scope, receive, send):
        tagged = dict(scope)
        tagged["corsia_listener"] = "devices"
        path = tagged.get("path", "")
        kind = tagged.get("type")
        may_reach = self._may_peer_reach_devices(tagged) or self._outside_enabled()
        if kind in {"http", "websocket"} and not may_reach:
            if kind == "http":
                await JSONResponse({"detail": {"reason": "not_on_this_network"}}, status_code=403)(tagged, receive, send)
            else:
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
            return
        if kind == "http" and self._requires_same_network(tagged):
            await JSONResponse({"detail": {"reason": "not_on_this_network"}}, status_code=403)(tagged, receive, send)
            return
        if (
            kind == "http"
            and (path == "/api" or path.startswith("/api/"))
            and (tagged.get("method"), path) not in _PUBLIC_API_ROUTES
            and not await self._has_human_session(tagged)
        ):
            await JSONResponse({"reason": "sign_in_required"}, status_code=401)(
                tagged, receive, send,
            )
            return
        # Browsers cannot set headers on a WebSocket, so the app authenticates
        # in its first frame, where the endpoints accept a person session only
        # on this listener. A token offered at the handshake is checked here,
        # before the socket is accepted.
        if kind == "websocket":
            offered, ambiguous = self._handshake_token(tagged)
            if (offered is not None or ambiguous) and not await self._has_human_session(tagged):
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})
                return
        try:
            await self._app(tagged, receive, send)
        except asyncio.CancelledError:
            # An explicit listener stop disconnects these requests by design.
            # Other cancellations retain their normal propagation semantics.
            if not self.stopping:
                raise


@dataclass(slots=True)
class DevicesListener:
    """Own the second server, its pre-bound socket, and its exposed port."""

    app: object
    server: DevicesServer | None = None
    task: asyncio.Task | None = None
    sock: socket.socket | None = None
    port: int | None = None
    error: str | None = None
    # Why it is not listening, in words the page can act on: ports_taken, credentials, bind, serve or disabled.
    reason: str | None = None
    announcement: DNSServiceAnnouncement | None = None
    # Told when the announced name or its error changes after start().
    on_announcement_change: Callable[[], None] | None = None
    outside_enabled: Callable[[], bool] | None = None

    @property
    def listening(self) -> dict:
        if self.port is not None:
            return {"port": self.port}
        return {"error": self.error, "reason": self.reason} if self.error else {"error": self.error}

    async def start(self) -> bool:
        if self.task is not None:
            return True
        try:
            key_path, cert_path = ensure_host_key()
        except Exception as exc:
            logger.error("Devices listener host credentials unavailable: %s", exc)
            self.error = "Devices listener credentials unavailable"
            self.reason = "credentials"
            return False
        sock, port, error = _prebind()
        if sock is None:
            self.error = error
            self.reason = "ports_taken" if error == _NO_PORT_ERROR else "bind"
            return False
        assert port is not None
        config = uvicorn.Config(
            DevicesApp(self.app, outside_enabled=self.outside_enabled),
            host="::",
            port=port,
            log_config=None,
            proxy_headers=False,
            lifespan="off",
            timeout_graceful_shutdown=2,
            ssl_keyfile=str(key_path),
            ssl_certfile=str(cert_path),
        )
        self.server = DevicesServer(config)
        self.sock = sock
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        self.task.add_done_callback(self._serve_ended)
        self.port = port
        self.error = None
        self.reason = None
        self.announcement = DNSServiceAnnouncement(port)
        self.announcement.on_change = self.on_announcement_change
        self.announcement.start()
        return True

    def _serve_ended(self, task: asyncio.Task) -> None:
        """Nothing awaits the serve task until stop(), so a failure (a TLS key that will not load, a crash) would
        leave `listening` naming a port nobody serves. A requested stop and a cancelled loop are expected."""
        if task is not self.task or task.cancelled() or (self.server is not None and self.server.should_exit):
            return
        logger.error("Devices listener stopped serving: %s", task.exception() or "the server exited")
        if self.announcement is not None:
            self.announcement.stop()
        if self.sock is not None:
            self.sock.close()
        self.announcement = self.server = self.task = self.sock = self.port = None
        self.error, self.reason = "Devices listener stopped serving", "serve"
        if self.on_announcement_change is not None:
            self.on_announcement_change()

    async def stop(self) -> None:
        if self.server is None or self.task is None:
            return
        if self.announcement is not None:
            self.announcement.stop()
            self.announcement = None
        self.server.should_exit = True
        try:
            await self.task
        finally:
            self.server = None
            self.task = None
            self.sock = None
            self.port = None


_NO_PORT_ERROR = "No approved Devices port is available"


def _prebind() -> tuple[socket.socket | None, int | None, str | None]:
    """Bind the approved port range without letting Uvicorn exit the backend."""
    for port in (DEFAULT_PORT, *FALLBACK_PORTS):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::", port))
            sock.listen(socket.SOMAXCONN)
            sock.setblocking(False)
            if port != DEFAULT_PORT:
                logger.info("Devices listener using fallback port %d", port)
            return sock, port, None
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                continue
            return None, None, exc.strerror or str(exc)
    return None, None, _NO_PORT_ERROR
