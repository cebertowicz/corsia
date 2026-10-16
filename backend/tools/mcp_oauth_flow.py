"""One person's sign-in to one hosted MCP server, from Sign in to a stored grant.

At most one pending flow per app (a second press reopens the same page). A flow
owns a loopback listener, a single-use ``state``, the PKCE verifier and the
starting person's session; it ends on a valid callback, the five-minute expiry,
a cancel, or the listener's request cap. Failures are fixed classes
(``tools.mcp_oauth_errors``); every outcome is audited with actor and app,
never a value.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from infra.db_async import run_in_db
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_secrets as mcp_oauth_secrets
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError
from tools.mcp_oauth_errors import SignInError
from tools.mcp_oauth_listener import CallbackListener

EXPIRY_SECONDS = 300.0
DEFAULT_PATH = "/callback"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_SUCCESS_PAGE = "{app} is connected to CORSIA. You can close this tab and go back to CORSIA."
_FAILURE_PAGE = "Sign-in didn't finish. Go back to CORSIA to see why."
_REFUSED_PAGE = "This isn't the sign-in CORSIA is waiting for."


def oauth_settings(config: dict) -> tuple[str | None, str, int, str]:
    """The pasted client ID and the callback host, port and path a server's config asks for.

    The host may only be a loopback name; anything else is refused before any listener opens.
    """
    client_id = config.get("oauth_client_id")
    if client_id is not None and (not isinstance(client_id, str) or not 0 < len(client_id) <= 512 or not client_id.isprintable() or " " in client_id):
        raise MCPConfigError("oauth_client_id must be the client ID text")
    callback = config.get("oauth_callback") or {}
    if not isinstance(callback, dict) or set(callback) - {"host", "port", "path"}:
        raise MCPConfigError("oauth_callback may set only host, port and path")
    host, port, path = callback.get("host", "127.0.0.1"), callback.get("port", 0), callback.get("path", DEFAULT_PATH)
    if host not in _LOOPBACK_HOSTS:
        raise MCPConfigError("the sign-in callback host must be 127.0.0.1, localhost or [::1]")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise MCPConfigError("the sign-in callback port must be a number from 1 to 65535")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 200 or "?" in path or "#" in path or not path.isprintable():
        raise MCPConfigError("the sign-in callback path must start with /")
    return client_id, host, port, path


@dataclass
class Flow:
    app_id: str
    app_name: str
    user_id: str
    session_hash: str
    server_url: str
    state: str
    verifier: str
    redirect_uri: str
    host: str
    disc: protocol.Discovery
    client: protocol.ClientRegistration
    authorization_url: str
    expires_at: float
    listener: CallbackListener | None = None
    consumed: bool = False
    timer: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class Result:
    failure_class: str
    words: str | None
    at: float


_flows: dict[str, Flow] = {}
_results: dict[str, Result] = {}
_lock = asyncio.Lock()
_background: set[asyncio.Task] = set()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def status(app_id: str) -> dict[str, object]:
    """The value-free sign-in status of one app: pending, or how the last attempt ended."""
    flow = _flows.get(app_id)
    if flow is not None:
        return {"status": "pending", "expires_at": _iso(flow.expires_at)}
    last = _results.get(app_id)
    if last is not None:
        return {"status": "failed", "failure_class": last.failure_class, "at": _iso(last.at)}
    return {"status": "idle"}


def pending_link(app_id: str) -> str | None:
    """The consent link of a pending flow, for the starting person's own screen only."""
    flow = _flows.get(app_id)
    return flow.authorization_url if flow else None


def last_words(app_id: str) -> str | None:
    last = _results.get(app_id)
    return last.words if last else None


def _allowed_hosts(flow_host: str):
    def hosts(port: int) -> set[str]:
        allowed = {f"127.0.0.1:{port}"}
        if flow_host != "127.0.0.1":
            allowed.add(f"{flow_host}:{port}")
        return allowed
    return hosts


async def _audit(app_id: str, action: str, actor: tuple[str, str]) -> None:
    from connections.app_lifecycle_support import record_event

    try:
        await run_in_db(record_event, app_id, action, actor=actor)
    except Exception:
        logger.warning("sign-in audit write failed for app {}", app_id)


async def start(
    app_id: str, app_name: str, config: dict, *, user_id: str, session_hash: str, metadata_url: str,
) -> dict[str, object]:
    """Begin a sign-in, or reopen the pending one (C-R8a). Returns the consent link and expiry."""
    async with _lock:
        existing = _flows.get(app_id)
        if existing is not None and existing.expires_at > time.time():
            return {"authorization_url": existing.authorization_url, "expires_at": _iso(existing.expires_at), "reopened": True}
        server_url = config.get("url")
        if not isinstance(server_url, str):
            raise MCPConfigError("only an app at a web address can be signed in to")
        pasted, host, port, path = oauth_settings(config)
        flow = await _open_flow(app_id, app_name, server_url, user_id, session_hash, pasted, host, port, path, metadata_url)
        _flows[app_id] = flow
        _results.pop(app_id, None)
        flow.timer = asyncio.create_task(_expire(flow))
    await _audit(app_id, "sign_in_started", ("person", user_id))
    return {"authorization_url": flow.authorization_url, "expires_at": _iso(flow.expires_at), "reopened": False}


async def _open_flow(
    app_id: str, app_name: str, server_url: str, user_id: str, session_hash: str, pasted: str | None,
    host: str, port: int, path: str, metadata_url: str,
) -> Flow:
    state = protocol.new_state()
    verifier, challenge = protocol.new_pkce()
    holder: dict[str, Flow] = {}

    async def handler(query: dict[str, str]):
        return await _callback(holder["flow"], query)

    async def on_cap() -> None:
        await _end(holder["flow"], SignInError("unknown"))

    listener = CallbackListener(path=path, allowed_hosts=_allowed_hosts(host), handler=handler, on_cap=on_cap, port=port)
    try:
        bound = await listener.start()
    except OSError:
        raise SignInError("address_mismatch") from None
    try:
        redirect_uri = f"http://{host}:{bound}{path}"
        async with protocol.new_client() as http:
            disc = await protocol.discover(http, server_url)
            client = await protocol.register_client(
                http, disc, redirect_uri, pasted_client_id=pasted, metadata_url=metadata_url,
            )
        mcp_oauth_secrets.register(client.client_secret, client.registration_access_token)
        link = protocol.authorization_url(disc, client_id=client.client_id, redirect_uri=redirect_uri, state=state, challenge=challenge)
    except BaseException:
        await listener.stop()
        raise
    flow = Flow(
        app_id, app_name, user_id, session_hash, server_url, state, verifier, redirect_uri, host, disc, client, link,
        time.time() + EXPIRY_SECONDS, listener,
    )
    holder["flow"] = flow
    return flow


async def _expire(flow: Flow) -> None:
    await asyncio.sleep(max(0.0, flow.expires_at - time.time()))
    await _end(flow, SignInError("timed_out"))


async def _end(flow: Flow, failure: SignInError | None) -> None:
    """Close a flow once: stop its listener, record how it ended, and forget its secrets' owner."""
    if _flows.get(flow.app_id) is not flow:
        return
    del _flows[flow.app_id]
    if flow.timer is not None and flow.timer is not asyncio.current_task():
        flow.timer.cancel()
    if flow.listener is not None:
        await flow.listener.stop()
        for kind, count in sorted(flow.listener.refused.items()):
            await _audit(flow.app_id, f"sign_in_refused:{kind}:{count}", ("system", "sign_in_listener"))
    if failure is None:
        return
    _results[flow.app_id] = Result(failure.failure_class, failure.words, time.time())
    action = "sign_in_expired" if failure.failure_class == "timed_out" else f"sign_in_failed:{failure.failure_class}"
    await _audit(flow.app_id, action, ("system", "sign_in"))


async def cancel(app_id: str, *, user_id: str) -> bool:
    """End a pending sign-in at the person's request. False when none was pending."""
    flow = _flows.get(app_id)
    if flow is None:
        return False
    del _flows[app_id]
    if flow.timer is not None:
        flow.timer.cancel()
    if flow.listener is not None:
        await flow.listener.stop()
    await _audit(app_id, "sign_in_cancelled", ("person", user_id))
    return True


def _session_valid(session_hash: str, user_id: str) -> bool:
    from infra.database import get_db
    from infra.time import utc_now_iso

    row = get_db().execute("SELECT user_id, expires_at FROM auth_tokens WHERE token_hash = ?", (session_hash,)).fetchone()
    return row is not None and row["user_id"] == user_id and not (row["expires_at"] and row["expires_at"] <= utc_now_iso())


async def _callback(flow: Flow, query: dict[str, str]) -> tuple[int, str, bool, str | None]:
    """Judge one request on the callback path: refuse it and keep listening, or finish the flow."""
    given = query.get("state", "")
    if flow.consumed or not given or not hmac.compare_digest(given.encode(), flow.state.encode()):
        return 400, _REFUSED_PAGE, False, "state"
    if flow.disc.iss_supported and query.get("iss") != flow.disc.issuer:
        return 400, _REFUSED_PAGE, False, "iss"
    if "error" in query:
        flow.consumed = True
        await _end(flow, protocol_error(query))
        return 200, _FAILURE_PAGE, True, None
    code = query.get("code")
    if not code:
        return 400, _REFUSED_PAGE, False, "code"
    mcp_oauth_secrets.register(code)
    flow.consumed = True
    failure = await _finish(flow, code)
    if failure is not None:
        await _end(flow, failure)
        return 200, _FAILURE_PAGE, True, None
    await _end(flow, None)
    await _audit(flow.app_id, "sign_in_completed", ("person", flow.user_id))
    _connect_in_background(flow.app_id)
    return 200, _SUCCESS_PAGE.format(app=flow.app_name), True, None


def protocol_error(query: dict[str, str]) -> SignInError:
    from tools.mcp_oauth_errors import from_provider_error

    return from_provider_error(query.get("error"), query.get("error_description"))


async def _finish(flow: Flow, code: str) -> SignInError | None:
    """Exchange the code and keep the grant for the person who started the flow. None on success."""
    if not await run_in_db(_session_valid, flow.session_hash, flow.user_id):
        return SignInError("unknown")
    try:
        async with protocol.new_client() as http:
            tokens = await protocol.exchange_code(
                http, flow.disc, client_id=flow.client.client_id, code=code, redirect_uri=flow.redirect_uri,
                verifier=flow.verifier, client_secret=flow.client.client_secret,
            )
            mcp_oauth_secrets.register(tokens.access_token, tokens.refresh_token)
            account = await protocol.account_label(http, flow.disc, tokens.access_token)
    except SignInError as error:
        return error
    grant = store.Grant(
        app_id=flow.app_id, user_id=flow.user_id, server_url=flow.server_url, issuer=flow.disc.issuer,
        resource=flow.disc.resource, token_endpoint=flow.disc.token_endpoint, client_id=flow.client.client_id,
        access_token=tokens.access_token, expires_at=tokens.expires_at,
        revocation_endpoint=flow.disc.revocation_endpoint, client_secret=flow.client.client_secret,
        registration_access_token=flow.client.registration_access_token, refresh_token=tokens.refresh_token,
        scope=tokens.scope, account=account,
    )
    try:
        await run_in_db(store.save_new, grant)
    except store.GrantStoreError:
        return SignInError("unknown")
    return None


def _connect_in_background(app_id: str) -> None:
    async def connect() -> None:
        try:
            from tools.mcp_runtime import apply_connected_app_runtime

            await apply_connected_app_runtime("sign_in", app_id)
        except Exception:
            logger.warning("connecting app {} after sign-in failed", app_id)

    task = asyncio.create_task(connect())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def stop_all() -> None:
    """End every pending flow (shutdown, tests)."""
    for flow in list(_flows.values()):
        await cancel(flow.app_id, user_id=flow.user_id)
    _results.clear()
