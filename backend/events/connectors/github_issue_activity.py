"""Webhook-shaped payloads for the issue and label activity the repo-activity poller reads.

``issues/events`` rows give labels, closes and reopens; GitHub has no row for an issue
being opened, so ``new_issues`` reads the issue list. Both build the payload GitHub's
webhook would send, so ``github_label_events.change_event`` normalizes the poller copy
and the webhook copy the same way. The event's own time stands where a webhook carries
the item's ``updated_at``: GitHub sets the two together (checked on recorded closed and
merged deliveries against their ``issues/events`` rows, one second for one second).
A pull request's head is the one the poller last saw for it, because the event row has none.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ROW_ACTIONS = ("labeled", "unlabeled", "closed", "reopened")
Admit = Callable[[str, dict[str, Any], str], Awaitable[None]]


def row_delivery(repo: str, row: dict[str, Any], heads: dict[str, str]) -> tuple[str, dict[str, Any], str] | None:
    """(webhook event, payload, delivery key) for one ``issues/events`` row, or ``None`` to skip it.

    A pull request's close and reopen are read from its own list, so only its label
    changes are built here.
    """
    action, issue, at = row.get("event"), row.get("issue"), row.get("created_at")
    if action not in ROW_ACTIONS or not isinstance(issue, dict) or not isinstance(at, str):
        return None
    is_pr = bool(issue.get("pull_request"))
    if is_pr and action not in ("labeled", "unlabeled"):
        return None
    item = {**issue, "updated_at": at, **({"closed_at": at} if action == "closed" else {})}
    payload: dict[str, Any] = {"action": action, "repository": {"full_name": repo},
                               ("pull_request" if is_pr else "issue"): item}
    if is_pr and (sha := heads.get(str(issue.get("number")))):
        item["head"] = {"sha": sha}
    if isinstance(row.get("label"), dict):
        payload["label"] = {"name": row["label"].get("name")}
    if isinstance(row.get("actor"), dict):
        payload["sender"] = {"login": row["actor"].get("login")}
    return ("pull_request" if is_pr else "issues"), payload, f"{action}:{repo}:{row.get('id')}"


async def new_issues(get: Callable[..., Awaitable[Any]], admit: Admit, repo: str, state: dict[str, Any],
                     now: Any) -> bool | None:
    """Admit issues opened since the saved time; the first read only seeds it.

    ``None`` when the budget or a backoff stops the read; ``False`` on a 304 or an error.
    """
    response = await get(f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=30", state, now)
    if response is None:
        return None
    if response.status != 200 or not isinstance(response.body, list):
        return False
    rows = [row for row in response.body if isinstance(row, dict) and not row.get("pull_request")
            and isinstance(row.get("created_at"), str)]
    seen, seeding = state.get("issues_opened_at") or "", "issues_opened_at" not in state
    state["issues_opened_at"] = max([seen, *(row["created_at"] for row in rows)])
    changed = False
    for row in sorted(rows, key=lambda r: r["created_at"]):
        if seeding or row["created_at"] <= seen:
            continue
        user: dict[str, Any] = row["user"] if isinstance(row.get("user"), dict) else {}
        await admit("issues", {"action": "opened", "repository": {"full_name": repo}, "issue": row,
                               "sender": {"login": user.get("login")}}, f"opened:{repo}#{row.get('number')}")
        changed = True
    return changed
