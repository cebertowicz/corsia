"""Policy metadata for External provider endpoints."""

from __future__ import annotations

from llm.external_provider import PREFIX
from llm.network_discovery import read_server_metadata


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
