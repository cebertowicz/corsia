"""Attach the app's stored sign-in to a hosted MCP server's requests, and keep it fresh.

The sign-in is the app's, so every agent the app reaches uses the same grant. The
access token is read from the key store into this object and goes out only as
an ``Authorization`` header on requests to the server's own origin (never after a
redirect: the clients here follow none), and only while the app's address keeps
the origin the grant was issued for: after the address moves, the server gets no
grant and reads ``needs_sign_in`` until the person signs in there. One refresh at a time per app runs under
the app's grant lock and is written as a single key-store transaction, so two
refreshes never overwrite a rotated refresh token. A rejected refresh, or a
rotated pair that cannot be written, ends the app's sign-in and moves the server
to ``needs_sign_in``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from loguru import logger

from infra.db_async import run_in_db
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_secrets as mcp_oauth_secrets
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError, url_origin

# Refresh a little before expiry; and never twice in this many seconds (a 401 storm).
_SKEW = 30.0
_COOLDOWN = 5.0
_background: set[asyncio.Task] = set()


def _has_challenge(response: httpx.Response) -> bool:
    return any("resource_metadata" in header for header in response.headers.get_list("www-authenticate"))


class OAuthGrantAuth(httpx.Auth):
    """Bearer auth from a stored sign-in; sends nothing when there is none."""

    def __init__(self, app_id: str, server_url: str) -> None:
        self._app_id = app_id
        self._server_url = server_url
        self._origin = url_origin(server_url)
        self._grant: store.Grant | None = None
        self._loaded = False
        self._last_refresh = float("-inf")
        #: Set when the server demanded a sign-in this object cannot satisfy; read by the connector.
        self.challenged = False

    async def _load(self) -> store.Grant | None:
        """The app's grant when it was issued for this server's origin; None otherwise."""
        grant = await run_in_db(store.load, self._app_id)
        return grant if grant is not None and grant.issued_for(self._server_url) else None

    async def _current(self) -> store.Grant | None:
        if not self._loaded:
            self._grant = await self._load()
            self._loaded = True
        return self._grant

    async def async_auth_flow(self, request: httpx.Request):
        if url_origin(str(request.url)) != self._origin:
            raise MCPConfigError("MCP endpoint changed origin; the sign-in was not sent")
        grant = await self._current()
        if grant is not None and grant.expires_at - _SKEW <= time.time():
            grant = await self._refresh(grant)
        if grant is not None:
            request.headers["Authorization"] = f"Bearer {grant.access_token}"
        response = yield request
        if response.status_code != 401:
            return
        if grant is None:
            await self._note_challenge(response, request)
            return
        fresh = await self._refresh(grant, force=True)
        if fresh is not None and fresh.access_token != grant.access_token:
            request.headers["Authorization"] = f"Bearer {fresh.access_token}"
            yield request

    async def _note_challenge(self, response: httpx.Response, request: httpx.Request) -> None:
        if _has_challenge(response):
            self.challenged = True
            return
        async with protocol.new_client() as http:
            self.challenged = await protocol.supports_oauth(http, str(request.url))

    async def _refresh(self, grant: store.Grant, *, force: bool = False) -> store.Grant | None:
        """Refresh once for everyone waiting; None when the sign-in is over."""
        async with store.grant_lock(grant.app_id):
            stored = await self._load()
            if stored is None:
                self._grant = None
                return None
            if stored.access_token != grant.access_token or (
                not force and stored.expires_at - _SKEW > time.time()
            ) or time.monotonic() - self._last_refresh < _COOLDOWN:
                self._grant = stored
                return stored
            if not stored.refresh_token:
                await self._lost(stored, "no_refresh_token")
                return None
            self._last_refresh = time.monotonic()
            try:
                async with protocol.new_client() as http:
                    tokens = await protocol.refresh_tokens(
                        http, token_endpoint=stored.token_endpoint, client_id=stored.client_id, server_url=stored.server_url,
                        refresh_token=stored.refresh_token, resource=stored.resource,
                        client_secret=stored.client_secret,
                    )
            except protocol.RefreshRejected:
                await self._lost(stored, "rejected")
                return None
            except protocol.RefreshUnavailable:
                await self._audit("refresh_failed:unavailable")
                return stored
            return await self._rotate(stored, tokens)

    async def _rotate(self, stored: store.Grant, tokens: protocol.Tokens) -> store.Grant | None:
        mcp_oauth_secrets.register(tokens.access_token, tokens.refresh_token)
        stored.access_token = tokens.access_token
        stored.refresh_token = tokens.refresh_token or stored.refresh_token
        stored.expires_at = tokens.expires_at
        stored.scope = tokens.scope or stored.scope
        stored.last_refreshed_at = time.time()
        stored.refresh_count += 1
        try:
            await run_in_db(store.rotate, stored)
        except store.GrantStoreError:
            await self._lost(stored, "write_failed")
            return None
        self._grant = stored
        return stored

    async def _lost(self, stored: store.Grant, why: str) -> None:
        """End the app's sign-in locally (the caller holds its lock), audit it, and move the server to needs_sign_in."""
        self._grant = None
        await run_in_db(store.end, stored.app_id)
        await self._audit(f"refresh_failed:{why}")
        _spawn(_report_lost(stored.app_id))
        return None

    async def _audit(self, action: str) -> None:
        from tools.mcp_oauth_flow import audit

        await audit(self._app_id, action, ("system", "oauth_refresh"))

    def redact(self, value):
        if isinstance(value, str):
            return mcp_oauth_secrets.redact(value)
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value


def _spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _report_lost(app_id: str) -> None:
    """Withdraw the server's tools and publish needs_sign_in outside the request that found out."""
    try:
        from tools.mcp_runtime import apply_connected_app_runtime

        await apply_connected_app_runtime("sign_out", app_id)
    except Exception:
        logger.warning("moving app {} to needs_sign_in failed", app_id)
