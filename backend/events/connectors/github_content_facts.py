"""Content-author and item-version facts for GitHub normalization."""
from __future__ import annotations

import re
from typing import Any

from events.guard import event_author
from events.markers import has_reviewer_marker
from events.connectors.github_label_events import label_names
from events.connectors.github_ownership import is_own
from events.connectors.github_screen.floor import edited_item_version


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


def author_origin(login: str | None) -> str:
    """``inside`` for CORSIA's own author, ``outside`` for anyone else or no one."""
    return "inside" if login and is_own(login) else "outside"


def content_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Facts about the authored item, without its title or body."""
    author = event_author(raw)
    login = author.get("login")
    login = login if isinstance(login, str) and login else None
    facts: dict[str, Any] = {"author_origin": author_origin(login)}
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
        version = edited_item_version(item, review=isinstance(raw.get("review"), dict))
    elif action == "opened":
        version = item.get("number")
    elif action == "submitted" and isinstance(raw.get("review"), dict):
        version = item.get("id") if isinstance(item.get("body"), str) and item["body"] else None
    else:
        version = item.get("id")
    if version is not None:
        facts["item_version"] = version
    return facts


REPLY_TYPES = frozenset({"pr_comment.created", "review.submitted"})
REPLY_FILTERABLE = ("repo", "item_version", "author", "author_origin", "author_login", "author_association",
                    "pr_number", "pr_author_login", "pr_author_origin", "has_reviewer_marker", "comment_id",
                    "review_id", "path", "commit_id", "in_reply_to_id", "state", "release_sha")
_RELEASE = re.compile(r"review: release ([0-9a-fA-F]{7,40})")


def _login(item: Any) -> str | None:
    user = item.get("user") if isinstance(item, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


def reply_envelope_facts(raw: dict[str, Any], facts: dict[str, Any], content: dict[str, Any],
                         payload_facts: tuple[str, ...]) -> None:
    """Add content and preserve one PR-fact source for each reply event type."""
    facts.update(content)
    kind = content_kind(raw)
    if kind in REPLY_TYPES:
        facts.update(reply_facts(raw))
        facts.pop("sha", None)
        if kind == "pr_comment.created":
            for name in payload_facts:
                facts.pop(name, None)


def reply_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Facts of a created PR comment or submitted review; never include its body."""
    review = raw.get("review") if isinstance(raw.get("review"), dict) else None
    item = review or (raw.get("comment") if isinstance(raw.get("comment"), dict) else None)
    pr = raw.get("pull_request") if isinstance(raw.get("pull_request"), dict) else None
    issue = raw.get("issue") if isinstance(raw.get("issue"), dict) else None
    parent = pr or (issue if issue and issue.get("pull_request") else None)
    if item is None or parent is None or raw.get("action") not in ("created", "submitted"):
        return {}
    body = item.get("body") if isinstance(item.get("body"), str) else ""
    owner = _login(parent)
    release = None if review else _RELEASE.fullmatch(body.split("\n", 1)[0].strip())
    facts: dict[str, Any] = {
        "author_login": _login(item), "author_association": item.get("author_association"),
        "pr_number": parent.get("number"), "pr_author_login": owner,
        "pr_author_origin": author_origin(owner) if owner else None,
        "has_reviewer_marker": has_reviewer_marker(body),
        "review_id" if review else "comment_id": item.get("id"),
        "commit_id": item.get("commit_id"), "path": item.get("path"),
        "in_reply_to_id": item.get("in_reply_to_id"),
        "state": item["state"].lower() if isinstance(item.get("state"), str) else None,
        "release_sha": release.group(1).lower() if release else None,
    }
    return {key: value for key, value in facts.items() if value is not None}
