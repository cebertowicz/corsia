"""Dedicated TLS listener for pairing and remote CORSIA devices."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import ipaddress
import logging
import os
import secrets
import socket
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

import psutil
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.responses import JSONResponse

from infra.auth import authenticate, is_person_session, token_from_request
from infra.config import STATE_ROOT
from infra.db_async import run_in_db
from infra.dns_sd import DNSServiceAnnouncement


DEFAULT_PORT = 47901
FALLBACK_PORTS = range(47902, 47911)
_PRIVATE_V4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_EXCLUDED_INTERFACE_PREFIXES = ("lo", "utun", "tailscale", "tun", "tap", "ppp", "vpn", "bridge")
logger = logging.getLogger(__name__)


def local_hostname() -> str:
    """Return the host's mDNS name without exposing interface state."""
    hostname = socket.gethostname().rstrip(".").lower()
    return hostname if hostname.endswith(".local") else f"{hostname}.local"


def _load_macos_frameworks():
    return (
        ctypes.CDLL("/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration"),
        ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"),
    )


def macos_computer_name() -> str | None:
    """Return macOS's user-configured computer name when it is available."""
    if sys.platform != "darwin":
        return None
    try:
        system_configuration, core_foundation = _load_macos_frameworks()
        copy_name = system_configuration.SCDynamicStoreCopyComputerName
        copy_name.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        copy_name.restype = ctypes.c_void_p
        name = copy_name(None, None)
        if not name:
            return None
        string_length = core_foundation.CFStringGetLength
        string_length.argtypes = [ctypes.c_void_p]
        string_length.restype = ctypes.c_long
        max_size = core_foundation.CFStringGetMaximumSizeForEncoding
        max_size.argtypes = [ctypes.c_long, ctypes.c_uint32]
        max_size.restype = ctypes.c_long
        get_string = core_foundation.CFStringGetCString
        get_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        get_string.restype = ctypes.c_bool
        release = core_foundation.CFRelease
        release.argtypes = [ctypes.c_void_p]
        release.restype = None
        try:
            buffer = ctypes.create_string_buffer(max_size(string_length(name), 0x08000100) + 1)
            if get_string(name, buffer, len(buffer), 0x08000100):
                return buffer.value.decode("utf-8")
        finally:
            release(name)
    except (AttributeError, OSError, UnicodeDecodeError, ValueError):
        return None
    return None


def remote_connect_name(announcement: DNSServiceAnnouncement | None) -> str:
    """Resolve the one name presented by every remote-connect surface."""
    if announcement is not None and announcement.announced_name:
        return announcement.announced_name
    return macos_computer_name() or local_hostname()


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


# The one Uvicorn release whose private ``_wait_tasks_to_complete`` the
# override below was written against; any other version uses Uvicorn's own.
_PINNED_UVICORN_VERSION = "0.49.0"


class DevicesServer(uvicorn.Server):
    """A Uvicorn server that never claims process signal handling."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def _wait_tasks_to_complete(self) -> None:
        if uvicorn.__version__ != _PINNED_UVICORN_VERSION:
            await super()._wait_tasks_to_complete()
            return
        # Uvicorn has already sent normal connection shutdown (including WS
        # code 1012). Active SSE responses have no natural end, so disabling
        # Devices must disconnect transports and join their ASGI requests.
        # A streaming generator can be waiting for its next event rather than
        # reading http.disconnect, so closing the socket alone cannot join it.
        cast("DevicesApp", self.config.app).stopping = True
        for connection in list(self.server_state.connections):
            # TLS close() can wait beyond grace for a peer's close_notify;
            # an explicit Devices stop must also release an unread SSE socket.
            connection.transport.abort()
        await asyncio.sleep(0)
        requests = list(self.server_state.tasks)
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        # These exact owners are now disconnected and joined. Uvicorn's
        # generic waiter would additionally wait for a TLS peer's handshake;
        # keep that external wait out of an explicit Devices stop.


_PUBLIC_API_ROUTES = frozenset({
    ("GET", "/api/health"),
    ("GET", "/api/version"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/devices/pair/start"),
    ("POST", "/api/devices/pair/finish"),
})


class DevicesApp:
    """Tag Devices scopes and limit their HTTP access to pairing-safe routes."""

    def __init__(self, app):
        self._app = app
        self.stopping = False

    @staticmethod
    def _handshake_token(scope) -> str | None:
        """Read the token the way the API routes do: ``?token=`` wins over Bearer."""
        headers = dict(scope.get("headers", ()))
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        query_token = (query.get("token") or [None])[0]
        return token_from_request(query_token, authorization)

    @classmethod
    async def _has_human_session(cls, scope) -> bool:
        user = await run_in_db(authenticate, cls._handshake_token(scope))
        return user is not None and is_person_session(user)

    async def __call__(self, scope, receive, send):
        tagged = dict(scope)
        tagged["corsia_listener"] = "devices"
        path = tagged.get("path", "")
        if (
            tagged.get("type") == "http"
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
        if (
            tagged.get("type") == "websocket"
            and self._handshake_token(tagged) is not None
            and not await self._has_human_session(tagged)
        ):
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
    port: int | None = None
    error: str | None = None
    announcement: DNSServiceAnnouncement | None = None

    @property
    def listening(self) -> dict:
        return {"port": self.port} if self.port is not None else {"error": self.error}

    async def start(self) -> bool:
        if self.task is not None:
            return True
        try:
            key_path, cert_path = ensure_host_key()
        except Exception as exc:
            logger.error("Devices listener host credentials unavailable: %s", exc)
            self.error = "Devices listener credentials unavailable"
            return False
        sock, port, error = _prebind()
        if sock is None:
            self.error = error
            return False
        assert port is not None
        config = uvicorn.Config(
            DevicesApp(self.app),
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
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        self.port = port
        self.error = None
        self.announcement = DNSServiceAnnouncement(port)
        self.announcement.start()
        return True

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
            self.port = None


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
    return None, None, "No approved Devices port is available"


def _key_paths(state_root: Path = STATE_ROOT) -> tuple[Path, Path]:
    folder = state_root / "devices"
    return folder / "host-key.pem", folder / "host-cert.pem"


def _secure_file(path: Path) -> None:
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
        raise RuntimeError("Devices host key files have unsafe ownership or permissions")


def _certificate_for_key(key) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CORSIA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _private_key_bytes(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def ensure_host_key(state_root: Path = STATE_ROOT) -> tuple[Path, Path]:
    """Load, or securely repair, the host's stable ECDSA identity."""
    key_path, cert_path = _key_paths(state_root)
    folder = key_path.parent
    key_exists = key_path.exists()
    cert_exists = cert_path.exists()
    if key_exists:
        _secure_file(key_path)
    if cert_exists:
        _secure_file(cert_path)
    if key_exists and cert_exists:
        return key_path, cert_path

    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    if key_exists:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        _write_private_file(cert_path, _certificate_for_key(key))
        return key_path, cert_path

    key = ec.generate_private_key(ec.SECP256R1())
    _write_private_file(key_path, _private_key_bytes(key))
    _write_private_file(cert_path, _certificate_for_key(key))
    return key_path, cert_path


def host_spki_sha256(state_root: Path = STATE_ROOT) -> bytes:
    """Return the SHA-256 binding for the listener's stable public key."""
    _, cert_path = ensure_host_key(state_root)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return digest.finalize()


def _write_private_file(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
