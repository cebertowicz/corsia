"""End a hosted app's sign-in: Sign out, or Remove the app. Here first, then at the provider.

The app's single grant is deleted locally before anything is sent, so its tokens
are gone from the store whatever the provider does; revocation (RFC 7009) follows
with a short timeout, and its response is never read into a log or error. A
failed revoke is audited and undoes nothing; the caller says so and links to
where the person can remove CORSIA themselves. Every event is audited with the
real actor: the person, or the agent's thread for an agent's Remove.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable

from infra.db_async import run_in_db
import tools.mcp_oauth_flow as flow
import tools.mcp_oauth_store as store
from tools.mcp_oauth_flow import Actor


async def _tell_provider(app_id: str, grant: store.Grant, event: str, actor: Actor) -> str | None:
    """Revoke an already-deleted grant and audit how that went. The provider's address when it wasn't told."""
    if grant.refresh_count:
        await flow.audit(app_id, f"refresh_coalesced:count={grant.refresh_count}", actor)
    failure = await flow.tell_provider(grant)
    await flow.audit(app_id, flow.revoke_outcome(event, grant, failure), actor)
    return None if failure is None else flow.provider_origin(grant.issuer)


async def sign_out(app_id: str, *, actor: Actor) -> dict[str, object]:
    """End the app's sign-in here, then revoke it. Returns whether the provider was told, and where to go if not."""
    await flow.cancel(app_id, actor=actor)
    async with store.grant_lock(app_id):
        grant = await run_in_db(store.end, app_id)
    if grant is None:
        await flow.audit(app_id, "sign_out_no_sign_in", actor)
        return {"had_sign_in": False, "provider_told": False, "provider_url": None}
    untold = await _tell_provider(app_id, grant, "sign_out", actor)
    return {"had_sign_in": True, "provider_told": untold is None, "provider_url": untold}


def delete_with_sign_in(
    app_id: str, delete: Callable[..., dict[str, object]], conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, object], store.Grant | None]:
    """Delete the app with ``delete(db=...)`` and end its sign-in in the same write. ``delete`` raises when the app is not there."""
    from infra.database import write_transaction

    with write_transaction(conn, scope="connected_app_remove_with_sign_in") as tx:
        return delete(db=tx), store.end(app_id, db=tx)


async def remove_app(
    app_id: str, run_delete: Callable[[], Awaitable[tuple[dict[str, object], store.Grant | None]]], *, actor: Actor,
) -> dict[str, object]:
    """Delete the app and its sign-in in one write, then revoke it. A failed delete ends and revokes nothing.

    ``run_delete`` runs :func:`delete_with_sign_in` on the caller's database path; only after it commits is any
    pending sign-in cancelled and the ended grant revoked.
    """
    async with store.grant_lock(app_id):
        result, grant = await run_delete()
    await flow.cancel(app_id, actor=actor)
    if grant is not None:
        await _tell_provider(app_id, grant, "remove", actor)
    return result
