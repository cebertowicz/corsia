"""Authorization for oMLX's public HTTP surfaces.

This is intentionally separate from ``omlx_endpoints.admin_request``: public
OpenAI-compatible endpoints use a Bearer token, while oMLX admin endpoints use
a signed-in cookie session.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class OmlxPublicAuthError(RuntimeError):
    """A registered public request cannot prove its current endpoint."""


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname or "", parsed.port


async def public_headers(cfg: dict, *, url: str | None = None) -> dict[str, str]:
    """Return a public Bearer header from the current registered key.

    A registered server always wins over a caller's cfg snapshot, so a rotated
    or removed stored key takes effect on the next request. Candidate keys are
    allowed only before registration (when no server id exists). A supplied URL
    for a registered request must keep the current endpoint's origin.
    """
    server_id = cfg.get("server_id")
    if not server_id:
        candidate = cfg.get("api_key")
        return {"Authorization": f"Bearer {candidate}"} if candidate else {}

    from infra.db_async import run_in_db

    origins, key = await run_in_db(_registered_origins_and_key, server_id)
    if not origins:
        # A sparse legacy cfg with an unknown server id stays headerless. Its
        # cfg key never authorizes a registered-server request.
        if key is None:
            return {}
        raise OmlxPublicAuthError("Server has no registered public endpoint")
    if url is not None and _origin(url) not in origins:
        raise OmlxPublicAuthError("oMLX endpoint changed before public request")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _registered_origins_and_key(server_id: str) -> tuple[set[tuple[str, str, int | None]], str | None]:
    """Read a registered server's current key and every authorized public origin."""
    from llm.network_discovery import get_server_metadata
    from llm.network_endpoints import _server_port, get_endpoints

    metadata = get_server_metadata(server_id)
    key = metadata.get("api_key") if metadata else None
    port = _server_port(server_id)
    endpoints = get_endpoints(server_id)
    if port is None or not endpoints:
        return set(), key
    origins = {("http", str(endpoint["host"]).lower(), port) for endpoint in endpoints}
    return origins, key
