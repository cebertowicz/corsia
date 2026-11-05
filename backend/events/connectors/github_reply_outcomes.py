"""Outcome drafts for GitHub reviews and comments: who answered, when, and on which head.

Reviews, line comments and conversation comments have separate id spaces. Redelivery
or a poller reading the same item produces the same key; edits and deletes map to
nothing. Only a dismissal uses the PR's updated_at because it has no time of its own.
Bodies and diff hunks never enter outcome attributes. Conversation comments have no head.
"""
from __future__ import annotations

from typing import Any

from events.connectors import github_outcome_input as shape
from events.connectors import github_outcome_measurements as measurements
from events.connectors.github_outcomes import OutcomeDraft, pr_subject
from events.connectors.github_reply_facts import ReplyItem, optional_text, reply_item_facts


def _stored(attrs: dict[str, Any]) -> dict[str, Any]:
    """Convert validated reply facts to the outcome store's attribute types once."""
    ids = {"pr_number", "issue_number", "review_id", "comment_id", "in_reply_to_id"}
    return {key: ("yes" if value else "no") if isinstance(value, bool) else str(value) if key in ids else value
            for key, value in attrs.items() if value is not None}


def _author_facts(reply: ReplyItem, parent: dict[str, Any], kind: str, at: str) -> dict[str, Any]:
    """Authorship of the item and its parent, with elapsed whole minutes."""
    return {
        f"{kind}_number": reply.parent_number,
        "author_login": reply.author_login, "author_association": reply.author_association,
        f"{kind}_author_login": reply.parent_author_login,
        f"{kind}_author_association": reply.parent_author_association,
        f"minutes_since_{kind}_created": measurements.minutes_between(parent.get("created_at"), at),
    }


def _pr_facts(reply: ReplyItem, pr: dict[str, Any], at: str) -> dict[str, Any]:
    """Review and line-comment facts, with the head the payload names."""
    head = pr.get("head")
    return {
        **_author_facts(reply, pr, "pr", at), "has_reviewer_marker": reply.has_reviewer_marker,
        "head_sha": optional_text(shape.mapping(head), "sha") if head is not None else None,
        "commit_id": reply.commit_id,
    }


def review_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One submitted or dismissed review."""
    action = payload["action"]
    review = shape.mapping(shape.required(payload, "review"))
    pr = shape.mapping(shape.required(payload, "pull_request"))
    reply = reply_item_facts(review, pr)
    at = shape.timestamp(shape.required(review, "submitted_at") if action == "submitted"
                         else shape.required(pr, "updated_at"))
    attrs = {"repo": repo, "review_id": reply.id, "state": reply.state, **_pr_facts(reply, pr, at)}
    return [OutcomeDraft(
        f"github:pr_review:{repo}:{reply.id}:{action}", f"github.pr.review.{action}",
        at, pr_subject(repo, reply.parent_number), _stored(attrs),
    )]


def review_comment_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One created line comment on a pull request."""
    comment = shape.mapping(shape.required(payload, "comment"))
    pr = shape.mapping(shape.required(payload, "pull_request"))
    reply = reply_item_facts(comment, pr)
    at = shape.timestamp(shape.required(comment, "created_at"))
    attrs = {
        "repo": repo, "comment_id": reply.id, **_pr_facts(reply, pr, at),
        "path": reply.path, "in_reply_to_id": reply.in_reply_to_id,
    }
    return [OutcomeDraft(
        f"github:pr_review_comment:{repo}:{reply.id}:created", "github.pr.review_comment.created",
        at, pr_subject(repo, reply.parent_number), _stored(attrs),
    )]


def issue_comment_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One created conversation comment, on a pull request or on a plain issue."""
    comment = shape.mapping(shape.required(payload, "comment"))
    issue = shape.mapping(shape.required(payload, "issue"))
    reply = reply_item_facts(comment, issue)
    at = shape.timestamp(shape.required(comment, "created_at"))
    kind = "pr" if issue.get("pull_request") is not None else "issue"
    attrs = {"repo": repo, "comment_id": reply.id, **_author_facts(reply, issue, kind, at)}
    if kind == "pr":
        attrs["has_reviewer_marker"] = reply.has_reviewer_marker
    return [OutcomeDraft(
        f"github:{kind}_comment:{repo}:{reply.id}:created", f"github.{kind}.comment.created",
        at, f"github:{kind}:{repo}#{reply.parent_number}", _stored(attrs),
    )]
