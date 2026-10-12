"""oMLX endpoint resolution and URL helpers."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx

_TIMEOUT = 10.0


def _resolve_endpoint(cfg: dict) -> tuple[str | None, int | None]:
    """Return ``(host, port)`` for this config."""
    host, port = cfg.get("host"), cfg.get("port")
    if host and port:
        return host, port
    sid = cfg.get("server_id")
    if not sid:
        return None, None
    try:
        from llm.network_discovery import get_server
        from llm.network_endpoints import get_active_endpoint, get_endpoints
        server = get_server(sid)
        if server:
            ep = get_active_endpoint(sid)
            if ep is None:
                eps = get_endpoints(sid)
                if eps:
                    _RANK = {"local": 0, "lan": 1}
                    eps_sorted = sorted(
                        eps, key=lambda e: (_RANK.get(e.get("kind"), 99), e.get("created_at", ""))
                    )
                    ep = eps_sorted[0]
            host = host or (ep["host"] if ep else None)
            port = port or (int(server["port"]) if server.get("port") else None)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(
            "omlx endpoint registry lookup unavailable: %s", e
        )
    return host, port


def _api_base(cfg: dict) -> str | None:
    """Public OpenAI-compat base. ``http://host:port/v1``"""
    host, port = _resolve_endpoint(cfg)
    if not host or not port:
        return None
    return f"http://{host}:{port}/v1"


def _admin_base(cfg: dict) -> str | None:
    """Admin base. ``http://host:port/admin/api``"""
    host, port = _resolve_endpoint(cfg)
    if not host or not port:
        return None
    return f"http://{host}:{port}/admin/api"


def health_url(host: str, port: int) -> str:
    """The oMLX health surface for one host -- the only place it is built."""
    return f"http://{host}:{port}/health"


def _health_url(cfg: dict) -> str | None:
    host, port = _resolve_endpoint(cfg)
    if not host or not port:
        return None
    return health_url(host, port)


ADMIN_KEY_NEEDED = "oMLX needs its admin key"
NO_ENDPOINT = "Server has no reachable endpoint"
_SESSION_COOKIE = "omlx_admin_session"


class OmlxAdminError(RuntimeError):
    """An admin call could not be made; the message is fixed and safe to show."""


# oMLX 0.4.1 admin routes (``require_admin``, omlx/admin/auth.py) accept only the
# ``omlx_admin_session`` cookie that ``POST /admin/api/login`` sets; they never
# accept the Bearer key. The cookie is a secret like the key: it lives here, in
# memory, keyed by server and a hash of the key, and is never logged or returned.
_sessions: dict[tuple[str, str], str] = {}
_login_locks: dict[str, asyncio.Lock] = {}


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def _admin_key(cfg: dict) -> str | None:
    """The caller's key, else the one registered for this server."""
    key = cfg.get("api_key")
    if key:
        return str(key)
    server_id = cfg.get("server_id")
    if not server_id:
        return None
    from infra.db_async import run_in_db
    from infra.key_store_servers import resolve_server

    stored = await run_in_db(resolve_server, server_id)
    return str(stored) if stored else None


async def _login(admin: str, key: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(f"{admin}/login", json={"api_key": key, "remember": False})
    except Exception:
        raise OmlxAdminError("oMLX is unreachable") from None
    if response.status_code != 200:
        raise OmlxAdminError(f"oMLX admin sign-in failed (oMLX HTTP {response.status_code})")
    cookie = response.cookies.get(_SESSION_COOKIE)
    if not cookie:
        raise OmlxAdminError("oMLX admin sign-in failed")
    return cookie


async def _session(slot: tuple[str, str], admin: str, key: str, stale: str | None) -> str:
    """The server's cookie, signing in at most once however many calls wait on it."""
    lock = _login_locks.setdefault(slot[0], asyncio.Lock())
    async with lock:
        cookie = _sessions.get(slot)
        if cookie and cookie != stale:
            return cookie
        cookie = await _login(admin, key)
        for other in [k for k in _sessions if k[0] == slot[0] and k != slot]:
            del _sessions[other]  # the registered key changed: its old cookie is dead
        _sessions[slot] = cookie
        return cookie


async def admin_request(
    cfg: dict, method: str, path: str, *, json: Any = None, timeout: float = _TIMEOUT,
) -> httpx.Response:
    """Call one oMLX admin route as the signed-in admin; the one path for every admin call.

    Raises ``OmlxAdminError`` with a fixed message when there is no key (no HTTP
    call is made), the server is unreachable, or sign-in is refused. A 401 on the
    call signs in again once; a second 401 is returned for the caller to report.
    """
    admin = _admin_base(cfg)
    if not admin:
        raise OmlxAdminError(NO_ENDPOINT)
    key = await _admin_key(cfg)
    if not key:
        raise OmlxAdminError(ADMIN_KEY_NEEDED)
    slot = (str(cfg.get("server_id") or admin), _key_digest(key))
    stale: str | None = None
    for _attempt in range(2):
        cookie = await _session(slot, admin, key, stale)
        kwargs: dict[str, Any] = {"headers": {"Cookie": f"{_SESSION_COOKIE}={cookie}"}}
        if json is not None:
            kwargs["json"] = json
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await getattr(client, method.lower())(f"{admin}{path}", **kwargs)
        if response.status_code != 401:
            return response
        stale = cookie
    return response


async def setup_admin_key(cfg: dict, key: str) -> bool:
    """Set the first admin key on a keyless oMLX; False when it already has one.

    oMLX allows this only while no key is set (``POST /admin/api/setup-api-key``,
    omlx/admin/routes.py) and answers 400 otherwise. The cookie it returns is
    dropped: the next admin call signs in with the key like any other.
    """
    admin = _admin_base(cfg)
    if not admin:
        raise OmlxAdminError(NO_ENDPOINT)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{admin}/setup-api-key", json={"api_key": key, "api_key_confirm": key},
            )
    except Exception:
        raise OmlxAdminError("oMLX is unreachable") from None
    if response.status_code == 200:
        return True
    if response.status_code == 400:
        return False
    raise OmlxAdminError(f"oMLX could not set its admin key (oMLX HTTP {response.status_code})")
