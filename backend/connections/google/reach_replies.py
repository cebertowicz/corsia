"""Google's replies about an account that is already connected, for an add that cannot make another.

Said to the person on the callback page and to the agent in ``google_connect``; ``oauth`` re-exports them.
"""

from __future__ import annotations

ALREADY_CONNECTED = (
    "{address} is already connected{reach}. To change where it works, use Where it works… on its row in "
    "Connected apps."
)
ALREADY_NEEDS_SIGN_IN = (
    "{address} is already connected, but it needs you to sign in again. Use Sign in on its row in "
    "Connected apps."
)
CONNECTED_NOT_FOR_PROJECT = (
    "{address} is connected, but not for {project}. To add {project}, use Where it works… on its row in "
    "Connected apps."
)
CONNECTED_NOT_FOR_ALL = (
    "{address} is connected, but not for all your projects. To make it work for all of them, use Where it works… "
    "on its row in Connected apps."
)
STILL_CONNECTED = "{address} is already connected."


def not_covered_reply(account: dict, adding_thread_id: str | None, db) -> str | None:  # type: ignore[no-untyped-def]
    """Explain when an existing account cannot serve the adding conversation's scope."""
    if adding_thread_id is None:
        return None
    from connections.connected_accounts import ConnectedAccountError, reach_for_adding_thread

    try:
        requested = reach_for_adding_thread(adding_thread_id, db=db)
    except ConnectedAccountError:
        return None
    reach = account["reach"]
    if not isinstance(reach, dict):
        return None
    if requested.mode == "all":
        return CONNECTED_NOT_FOR_ALL.format(address=account["address"]) if reach.get("mode") != "all" else None
    project_id = next(iter(requested.project_ids), None)
    if project_id is None or reach.get("mode") == "all" or project_id in reach.get("project_ids", []):
        return None
    project = db.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        return None
    return CONNECTED_NOT_FOR_PROJECT.format(address=account["address"], project=project["name"])
