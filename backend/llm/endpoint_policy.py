"""Policy metadata for External provider endpoints."""

from __future__ import annotations

from urllib.parse import urlsplit

from llm.external_provider import PREFIX
from llm.network_discovery import parse_server_metadata, read_server_metadata


_ALLOWED_RESTRICTIONS = {"allowed", "interactive_only"}


def endpoint_agent_use(model_id: str) -> str:
    """Return an External endpoint's agent-use policy.

    An endpoint that declared nothing is ``allowed``. Metadata that cannot be read, or a
    restriction value outside the known set, is ``interactive_only``: an unreadable
    usage term must not let unattended work run on the endpoint.
    """
    if not model_id.startswith(PREFIX):
        return "allowed"
    server_id, sep, _ = model_id.removeprefix(PREFIX).partition("/")
    if not sep:
        return "allowed"
    metadata = read_server_metadata(server_id)
    if metadata is None:
        return "interactive_only"
    restriction = metadata.get("agent_use_restriction", "allowed")
    return restriction if restriction in _ALLOWED_RESTRICTIONS else "interactive_only"


def registration_default(base_url: str) -> str:
    """The restriction a new endpoint is stored with when its registration names none.

    A Token Plan endpoint, recognised by its host, is ``interactive_only``: its terms
    forbid unattended use. The endpoint's name is free text and never decides it. This
    is a default written once, as data; runtime reads only the stored value, and a
    registration that names a restriction keeps its own.
    Migration 109→110 applied the same rule to endpoints that already existed, and
    110→111 cleared the restriction an earlier name match had stored.
    """
    host = urlsplit(base_url).hostname or ""
    if "token-plan" in host.casefold():
        return "interactive_only"
    return "allowed"


def relocation_restriction(base_url: str) -> str | None:
    """The restriction an edit that moves an endpoint writes when the edit names none.

    Moving onto a host whose ``registration_default`` restricts it tightens the stored
    value, because the usage terms belong to the new host; any other move keeps it.
    """
    default = registration_default(base_url)
    return default if default != "allowed" else None


def registration_restriction(db, server_id: str, base_url: str) -> str | None:
    """The restriction to write when a registration names none, or None to write nothing.

    A stored restriction is kept; an endpoint with none gets ``registration_default``
    when that restricts it. Unreadable metadata counts as none: the registration
    rewrites the blob, so a restriction left to it would be lost.
    """
    row = db.execute("SELECT metadata_json FROM network_servers WHERE id = ?", (server_id,)).fetchone()
    stored = parse_server_metadata(server_id, row["metadata_json"] if row else None)
    if stored and "agent_use_restriction" in stored:
        return None
    default = registration_default(base_url)
    return default if default != "allowed" else None
