"""Strict reply-item reads shared by GitHub events and stored outcomes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from events.connectors import github_outcome_input as shape
from events.connectors.github_content_facts import author_origin
from events.identity_formula import MalformedEventError
from events.markers import has_reviewer_marker

REPLY_TYPES = frozenset({"pr_comment.created", "review.submitted"})
REPLY_FILTERABLE = ("repo", "item_version", "author", "author_origin", "author_association",
                    "pr_number", "pr_author_login", "pr_author_origin", "has_reviewer_marker", "comment_id",
                    "review_id", "path", "commit_id", "in_reply_to_id", "state", "release_sha")
_RELEASE = re.compile(r"review: release ([0-9a-fA-F]{7,40})")


@dataclass(frozen=True)
class ReplyItem:
    """Validated source values; callers choose their event or storage vocabulary."""

    id: int
    parent_number: int
    author_login: str | None
    author_association: str | None
    parent_author_login: str | None
    parent_author_association: str | None
    body: str
    has_reviewer_marker: bool
    commit_id: str | None
    path: str | None
    in_reply_to_id: int | None
    state: str | None


def optional_text(item: dict[str, Any], key: str) -> str | None:
    """Read optional nonempty text, refusing a present value of another kind."""
    return None if item.get(key) is None else shape.text(item[key])


def _login(item: dict[str, Any]) -> str | None:
    user = item.get("user")
    return None if user is None else shape.text(shape.required(shape.mapping(user), "login"))


def reply_item_facts(item: dict[str, Any], parent: dict[str, Any]) -> ReplyItem:
    """Read each consumed field once; a deleted user's null identity is absent."""
    body = shape.body(item.get("body"))
    state = optional_text(item, "state")
    return ReplyItem(
        id=shape.number(shape.required(item, "id")),
        parent_number=shape.number(shape.required(parent, "number")),
        author_login=_login(item), author_association=optional_text(item, "author_association"),
        parent_author_login=_login(parent), parent_author_association=optional_text(parent, "author_association"),
        body=body, has_reviewer_marker=has_reviewer_marker(body),
        commit_id=optional_text(item, "commit_id"), path=optional_text(item, "path"),
        in_reply_to_id=None if item.get("in_reply_to_id") is None else shape.number(item["in_reply_to_id"]),
        state=state.lower() if state is not None else None,
    )


def reply_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Reply-event facts on a PR; never expose the body or stored-outcome names."""
    review = isinstance(raw.get("review"), dict)
    item = raw["review" if review else "comment"]
    parent = raw.get("pull_request") or raw.get("issue")
    try:
        reply = reply_item_facts(shape.mapping(item), shape.mapping(parent))
    except shape.PayloadRefusal as exc:
        raise MalformedEventError(f"invalid GitHub reply: {exc}") from exc
    facts: dict[str, Any] = {
        "pr_number": reply.parent_number, "pr_author_login": reply.parent_author_login,
        "pr_author_origin": author_origin(reply.parent_author_login) if reply.parent_author_login else None,
        "author_association": reply.author_association, "has_reviewer_marker": reply.has_reviewer_marker,
        "review_id" if review else "comment_id": reply.id,
        "commit_id": reply.commit_id,
    }
    if review:
        facts["state"] = reply.state
    else:
        facts.update(path=reply.path, in_reply_to_id=reply.in_reply_to_id)
        if release := _RELEASE.fullmatch(reply.body.split("\n", 1)[0].strip()):
            facts["release_sha"] = release.group(1).lower()
    return {key: value for key, value in facts.items() if value is not None}
