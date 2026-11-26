"""Who may use which library item, and where a copy lives: the author rule and the project-scope rule."""

from __future__ import annotations

from typing import Any

COMMUNITY_OFF = (
    "The person hasn't agreed to use what other CORSIA users have shared (the 'Use community improvements' line in their profile), "
    "and {name} was shared by someone other than CORSIA, so nothing was installed, updated or fetched. CORSIA's own library items still work. "
    "Continue without it, and write your own skill with skill skill-creator if you need one. "
    "Only the person changes this answer; don't ask them for it just for this task. remove still works on a copy you already hold."
)
COMMUNITY_OFF_NOTE = (
    "Only CORSIA's own items are shown: the person hasn't agreed to use what other CORSIA users have shared. "
    "Don't ask them to change that just for this task."
)


def community_refusal(item: dict[str, Any], community: bool) -> dict[str, str] | None:
    """Refuse an item someone other than CORSIA shared when the person said no to those."""
    from library.items import corsia_authored

    if community or corsia_authored(item):
        return None
    return {"error": "community_off", "message": COMMUNITY_OFF.format(name=item["name"])}


def project_refusal(owner_id: str, project_id: str | None, db) -> dict[str, str] | None:
    """Only the project's lead agent installs, updates or removes a copy every agent in it sees."""
    from infra.conductor import get_scope_coordinator

    if not project_id:
        return {"error": "no_project", "message": "scope project needs a thread in a project; use the default scope"}
    if get_scope_coordinator(project_id, db=db) != owner_id:
        return {
            "error": "not_lead_agent",
            "message": "only the project's lead agent changes a skill for the whole project; install it for yourself with the default scope, "
            "or propose it to the lead agent with the skill name and why",
        }
    return None


def external_id(owner_id: str, project_id: str | None, name: str, scope: str) -> str:
    """One registration per copy: a thread's own, or its project's."""
    return f"project/{project_id}/{name}" if scope == "project" else f"{owner_id}/{name}"
