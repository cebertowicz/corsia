"""Policy metadata for External provider endpoints."""

from __future__ import annotations

from llm.network_discovery import get_server_metadata


_ALLOWED_RESTRICTIONS = {"allowed", "interactive_only"}


def endpoint_agent_use(model_id: str) -> str:
    """Return an External endpoint's agent-use policy, defaulting safely to allowed."""
    if not model_id.startswith("external@"):
        return "allowed"
    tail = model_id[len("external@") :]
    if "/" not in tail:
        return "allowed"
    restriction = get_server_metadata(tail.split("/", 1)[0]).get("agent_use_restriction")
    return restriction if restriction in _ALLOWED_RESTRICTIONS else "allowed"
