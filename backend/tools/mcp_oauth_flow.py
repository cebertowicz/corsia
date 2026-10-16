"""One person's sign-in to one hosted MCP server, from Sign in to the app's stored grant.

The grant belongs to the app (``tools.mcp_oauth_store``); a pending flow belongs to
the session that started it. At most one flow is pending per app: the same
session pressing Sign in again reopens its own page (C-R8a), and another
session's press ends it and starts that session's own, so no session is ever
handed another's consent link. A flow owns a loopback listener, a single-use
``state``, the PKCE verifier and the starting session; it ends on a valid
callback, the five-minute expiry, a cancel, a newer flow, or the listener's
request cap. Only the app's current flow may store a grant; ``store.grant_lock``
guards both, so a cancel or another session's press lands wholly before a
sign-in's write (nothing stored) or after it (nothing left to end). A completed sign-in
replaces the app's grant and only then revokes the one it replaced; a
cancelled, expired or failed one leaves the existing grant working. Failures are
fixed classes (``tools.mcp_oauth_errors``); every outcome is audited with its
actor and app, never a value.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from infra.db_async import run_in_db
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_secrets as mcp_oauth_secrets
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError, url_origin
from tools.mcp_oauth_errors import SignInError
from tools.mcp_oauth_listener import CallbackListener, allowed_hosts, oauth_settings

EXPIRY_SECONDS = 300.0
_SUCCESS_PAGE = "{app} is connected to CORSIA. You can close this tab and go back to CORSIA."
_FAILURE_PAGE = "Sign-in didn't finish. Go back to CORSIA to see why."
_REFUSED_PAGE = "This isn't the sign-in CORSIA is waiting for."

#: Who acted, as the audit trail records it: ("person", user id), ("agent", thread id) or ("system", what).
Actor = tuple[str, str]


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


def _started_by(flow: Flow, user_id: str, session_hash: str) -> bool:
    return flow.user_id == user_id and hmac.compare_digest(flow.session_hash.encode(), session_hash.encode())


def pending_link(app_id: str, *, user_id: str, session_hash: str) -> str | None:
    """The consent link of a pending flow, for the session that started it and no other."""
    flow = _flows.get(app_id)
    return flow.authorization_url if flow is not None and _started_by(flow, user_id, session_hash) else None


def last_words(app_id: str) -> str | None:
    last = _results.get(app_id)
    return last.words if last else None


async def audit(app_id: str, action: str, actor: Actor) -> None:
    """Record one value-free sign-in event. A failed write is logged, not raised: it must not undo the sign-in."""
    from connections.app_lifecycle_support import record_sign_in_event

    try:
        await run_in_db(record_sign_in_event, app_id, action, actor=actor)
    except Exception:
        logger.warning("sign-in audit write failed for app {}", app_id)


def provider_origin(url: str) -> str:
    """``url``'s scheme, host and any non-default port: where a person finds the provider."""
    origin = url_origin(url)
    if origin is None:
        return ""
    scheme, host, port = origin
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")


async def tell_provider(grant: store.Grant) -> str | None:
    """Revoke an ended grant at its provider (RFC 7009). None once told, else ``protocol.revoke``'s reason."""
    token, hint = (grant.refresh_token, "refresh_token") if grant.refresh_token else (grant.access_token, "access_token")
    async with protocol.new_client(timeout=protocol.REVOKE_TIMEOUT) as http:
        return await protocol.revoke(
            http, grant.revocation_endpoint, token=token, token_type_hint=hint, client_id=grant.client_id,
            server_url=grant.server_url, client_secret=grant.client_secret,
        )


def revoke_outcome(event: str, grant: store.Grant, failure: str | None) -> str:
    """The audit action for revoking an ended grant; on failure, the reason and the provider, never a token."""
    return f"{event}_revoked" if failure is None else f"{event}_revoke_failed:{failure}:{provider_origin(grant.issuer)}"


async def start(
    app_id: str, app_name: str, config: dict, *, user_id: str, session_hash: str, metadata_url: str,
) -> dict[str, object]:
    """Begin this session's sign-in, or reopen its own pending one (C-R8a); either clears the last failure.

    Holds the app's lock throughout, so starts are one at a time and no grant is written meanwhile.
    """
    async with store.grant_lock(app_id):
        _results.pop(app_id, None)
        existing = _flows.get(app_id)
        if existing is not None and existing.expires_at > time.time() and _started_by(existing, user_id, session_hash):
            return {"authorization_url": existing.authorization_url, "expires_at": _iso(existing.expires_at), "reopened": True}
        server_url = config.get("url")
        if not isinstance(server_url, str):
            raise MCPConfigError("only an app at a web address can be signed in to")
        pasted, host, port, path = oauth_settings(config)
        if existing is not None and _detach(existing):
            # An expired flow, or another session's: it ends, and its callback is refused, before this one takes
            # its place (and, with a fixed callback port, before this one's listener needs the port).
            await _wind_down(existing)
            if existing.expires_at <= time.time():
                await audit(app_id, "sign_in_expired", ("system", "sign_in"))
            else:
                await audit(app_id, "sign_in_superseded", ("person", user_id))
        flow = await _open_flow(app_id, app_name, server_url, user_id, session_hash, pasted, host, port, path, metadata_url)
        _flows[app_id] = flow
        flow.timer = asyncio.create_task(_expire(flow))
    await audit(app_id, "sign_in_started", ("person", user_id))
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

    listener = CallbackListener(path=path, allowed_hosts=allowed_hosts(host), handler=handler, on_cap=on_cap, port=port)
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


def _detach(flow: Flow) -> bool:
    """Forget ``flow`` if it is still its app's current one; False when it had already ended. Caller holds the lock."""
    if _flows.get(flow.app_id) is not flow:
        return False
    del _flows[flow.app_id]
    return True


async def _close(flow: Flow) -> bool:
    """Forget a flow once, under the app's lock, and wind it down. False when it had already ended."""
    async with store.grant_lock(flow.app_id):
        detached = _detach(flow)
    if detached:
        await _wind_down(flow)
    return detached


async def _wind_down(flow: Flow) -> None:
    """Stop a forgotten flow's timer and listener, and audit what the listener refused."""
    if flow.timer is not None and flow.timer is not asyncio.current_task():
        flow.timer.cancel()
    if flow.listener is not None:
        await flow.listener.stop()
        for kind, count in sorted(flow.listener.refused.items()):
            await audit(flow.app_id, f"sign_in_refused:{kind}:{count}", ("system", "sign_in_listener"))


async def _end(flow: Flow, failure: SignInError | None) -> None:
    """Close a flow once and record how it ended."""
    if not await _close(flow) or failure is None:
        return
    _results[flow.app_id] = Result(failure.failure_class, failure.words, time.time())
    action = "sign_in_expired" if failure.failure_class == "timed_out" else f"sign_in_failed:{failure.failure_class}"
    await audit(flow.app_id, action, ("system", "sign_in"))


async def cancel(app_id: str, *, actor: Actor) -> bool:
    """End a pending sign-in and forget the last failure: Cancel, sign-out, Remove or shutdown. False if none pending.

    A cancel that arrives while a completing sign-in writes its grant waits for the write, then finds nothing pending.
    """
    async with store.grant_lock(app_id):
        _results.pop(app_id, None)
        flow = _flows.get(app_id)
        if flow is None or not _detach(flow):
            return False
    await _wind_down(flow)
    await audit(app_id, "sign_in_cancelled", actor)
    return True


def _session_valid(session_hash: str, user_id: str) -> bool:
    """Whether the session that started a flow is still the same person's live session, resolved like any request."""
    from infra.accounts import resolve_token_digest
    from infra.auth import is_person_session

    user = resolve_token_digest(session_hash)
    return user is not None and user["id"] == user_id and is_person_session(user)


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
    await audit(flow.app_id, "sign_in_completed", ("person", flow.user_id))
    _connect_in_background(flow.app_id)
    return 200, _SUCCESS_PAGE.format(app=flow.app_name), True, None


def protocol_error(query: dict[str, str]) -> SignInError:
    from tools.mcp_oauth_errors import from_provider_error

    return from_provider_error(query.get("error"), query.get("error_description"))


async def _finish(flow: Flow, code: str) -> SignInError | None:
    """Exchange the code and keep the grant as the app's, signed in by the flow's starter. None on success.

    Nothing is stored unless this is still the app's current flow and the app still exists; the flow is checked
    and ended in the same hold of the app's lock as the write. Only once the new grant is stored is the one it
    replaced revoked.
    """
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
        app_id=flow.app_id, signed_in_by=flow.user_id, server_url=flow.server_url, issuer=flow.disc.issuer,
        resource=flow.disc.resource, token_endpoint=flow.disc.token_endpoint, client_id=flow.client.client_id,
        access_token=tokens.access_token, expires_at=tokens.expires_at,
        revocation_endpoint=flow.disc.revocation_endpoint, client_secret=flow.client.client_secret,
        registration_access_token=flow.client.registration_access_token, refresh_token=tokens.refresh_token,
        scope=tokens.scope, account=account,
    )
    async with store.grant_lock(flow.app_id):
        if _flows.get(flow.app_id) is not flow:
            return SignInError("cancelled")  # superseded or cancelled while the code was exchanged
        try:
            replaced = await run_in_db(_keep, grant)
        except store.GrantStoreError:
            return SignInError("unknown")
        _detach(flow)
    await _wind_down(flow)
    if replaced is not None:
        await _retire(replaced, grant)
    return None


def _keep(grant: store.Grant) -> store.Grant | None:
    """Store ``grant`` as its app's in one write that first checks the app still exists. Returns the one replaced."""
    from infra.database import write_transaction

    with write_transaction(None, scope="mcp_sign_in_keep") as conn:
        if conn.execute("SELECT 1 FROM connected_apps WHERE id = ?", (grant.app_id,)).fetchone() is None:
            raise store.GrantStoreError("the app was removed")
        return store.replace(grant, db=conn)


async def _retire(old: store.Grant, new: store.Grant) -> None:
    """Revoke the grant a completed sign-in replaced; its tokens are already gone from the store."""
    actor = ("person", new.signed_in_by)
    unknown = old.account is None or new.account is None
    if old.client_id == new.client_id and (unknown or old.account == new.account):
        # Providers may revoke per client and account, which would end the new sign-in too. An unknown account is
        # never taken as the same one: the old grant is still kept unrevoked, but audited apart.
        same = "account_unknown" if unknown else "and_account"
        await audit(new.app_id, f"sign_in_replaced_not_revoked:same_client_{same}", actor)
        return
    await audit(new.app_id, revoke_outcome("sign_in_replaced", old, await tell_provider(old)), actor)


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
    """End every pending flow, closing its listener, and join its timer and any connect it began (shutdown, tests)."""
    flows = list(_flows.values())
    for flow in flows:
        await cancel(flow.app_id, actor=("system", "shutdown"))
    tasks = [flow.timer for flow in flows if flow.timer is not None] + list(_background)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _results.clear()
