"""Person-facing representation of a saved-login fill approval."""
from typing import Any

from host_access.browser_domain import url_domain


def public(item: Any, maximum: int) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": "fill",
        "state": item.state,
        "thread_id": item.thread_id,
        "root_thread_id": item.root_thread_id,
        "agent": item.agent,
        "site": item.site,
        "domain": url_domain(item.site),
        "username": item.username,
        "revision": item.revision,
        "application_hash": item.application_hash,
        "max_duration_seconds": maximum,
        "requested_duration_seconds": item.requested_duration_seconds,
        "duration_seconds": item.duration_seconds,
        "standing": item.standing,
        "expires_at": item.expires_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
