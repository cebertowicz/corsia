"""Sign out of a hosted MCP server: tell the provider, then forget locally whatever it says.

Revocation (RFC 7009) is attempted first with a short timeout and its response is
never read into a log or error. Every sign-in entry for the app is deleted
whether or not the provider answered; when it did not, the caller says so and
links to where the person can remove CORSIA themselves.
"""

from __future__ import annotations

from loguru import logger

from infra.db_async import run_in_db
import tools.mcp_oauth_flow as flow
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_store as store


async def _audit(app_id: str, action: str, user_id: str) -> None:
    from connections.app_lifecycle_support import record_event

    try:
        await run_in_db(record_event, app_id, action, actor=("person", user_id))
    except Exception:
        logger.warning("sign-out audit write failed for app {}", app_id)


async def _revoke(grant: store.Grant) -> bool:
    token = grant.refresh_token or grant.access_token
    # protocol.revoke answers False for every transport failure itself; nothing here needs a blanket catch.
    async with protocol.new_client(timeout=protocol.REVOKE_TIMEOUT) as http:
        return await protocol.revoke(http, grant.revocation_endpoint, token=token, client_id=grant.client_id)


async def sign_out(app_id: str, *, user_id: str) -> dict[str, object]:
    """Revoke, delete and audit. Returns whether the provider was told, and where to remove CORSIA if not."""
    await flow.cancel(app_id, user_id=user_id)
    grant = await run_in_db(store.load, app_id)
    revoked = await _revoke(grant) if grant is not None else False
    await run_in_db(store.delete_all, app_id)
    if grant is not None and grant.refresh_count:
        await _audit(app_id, f"refresh_coalesced:count={grant.refresh_count}", user_id)
    await _audit(app_id, "sign_out_revoked" if revoked else "sign_out_local_only", user_id)
    return {
        "had_sign_in": grant is not None,
        "provider_told": revoked or grant is None,
        "provider_url": _origin_url(grant.issuer) if grant is not None and not revoked else None,
    }


def _origin_url(url: str) -> str:
    scheme, host, port = protocol.origin_of(url)
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")
