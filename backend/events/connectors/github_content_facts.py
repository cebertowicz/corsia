"""Content-author and item-version facts for GitHub normalization."""
from __future__ import annotations

from typing import Any

from events.guard import event_author
from events.connectors.github_label_events import label_names
from events.connectors.github_ownership import is_own


def repository_name(raw: dict[str, Any]) -> Any:
    """The repository fact, without reading author identity."""
    repository = raw.get("repository")
    return repository.get("full_name") if isinstance(repository, dict) else None


def content_kind(raw: dict[str, Any]) -> str | None:
    """The kind of a review, a comment or an issue edit; None for anything else."""
    action = raw.get("action", "")
    if isinstance(raw.get("review"), dict):
        return f"review.{action}"
    if isinstance(raw.get("comment"), dict):
        issue = raw.get("issue")
        on_pr = raw.get("pull_request") or (isinstance(issue, dict) and issue.get("pull_request"))
        return f"{'pr_comment' if on_pr else 'issue_comment'}.{action}"
    if raw.get("issue") and action == "edited":
        return "issue.edited"
    return None


def content_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Facts about the authored item, without its title or body."""
    author = event_author(raw)
    login = author.get("login")
    login = login if isinstance(login, str) and login else None
    origin = "inside" if login and is_own(login) else "outside"
    facts: dict[str, Any] = {"author_origin": origin}
    if login:
        facts["author"] = login
    item: dict[str, Any] = next((raw[k] for k in ("comment", "review", "pull_request", "issue")
                 if isinstance(raw.get(k), dict)), {})
    container = raw.get("pull_request") or raw.get("issue") or {}
    if not isinstance(container, dict):
        container = {}
    if raw.get("issue") and not raw.get("pull_request"):
        facts["issue_number"] = container.get("number")
    if isinstance(container.get("labels"), list):
        facts["labels"] = label_names(container["labels"])
    if link := item.get("html_url"):
        facts["link"] = link
    if isinstance(item.get("body"), str):
        facts["size"] = len(item["body"])
    action = raw.get("action")
    head = container.get("head")
    if isinstance(head, dict) and action in ("opened", "synchronize"):
        version = head.get("sha")
    elif action == "edited":
        item_id, updated = item.get("id"), item.get("updated_at")
        version = f"{item_id}:{updated}" if item_id is not None and updated else None
    elif action == "opened":
        version = item.get("number")
    else:
        version = item.get("id")
    if version is not None:
        facts["item_version"] = version
    return facts
