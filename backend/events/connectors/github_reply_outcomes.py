"""Outcome drafts for GitHub reviews and comments: who answered, when, and on which head.

``github_outcomes`` hands a ``pull_request_review`` (``submitted``, ``dismissed``),
``pull_request_review_comment`` or ``issue_comment`` (``created``) payload here. Each is
keyed on the id GitHub gives the review or comment, so a redelivery, or the notifications
poller reading the same comment, is one event; an edit or a delete maps to nothing. The key
also names the kind: reviews, line comments and conversation comments are separate id spaces.

Facts are logins, an author association, ids, a path, a state, a count of minutes from
the item's own ``created_at`` to the event's time in the same payload, and whether the
body carries a published reviewer marker. No body, title or diff hunk is read into a fact.
A dismissal carries no time of its own, so it takes the pull request's ``updated_at``.
``issue_comment`` carries no head, so a pull request comment records none.
"""
from __future__ import annotations

from typing import Any

from events.connectors import github_outcome_input as shape
from events.connectors import github_outcome_measurements as measurements
from events.connectors.github_outcomes import OutcomeDraft, pr_subject
from events.connectors.github_content_facts import reply_item_facts


def _validate_user(item: dict[str, Any]) -> None:
    user = item.get("user")
    if user is not None:
        shape.text(shape.required(shape.mapping(user), "login"))


def _optional_text(item: dict[str, Any], key: str) -> str | None:
    return None if item.get(key) is None else shape.text(item[key])


def _shared(item: dict[str, Any], parent: dict[str, Any], *, review: bool, pr: bool = True) -> dict[str, Any]:
    _validate_user(item)
    _validate_user(parent)
    if pr:
        shape.body(item.get("body"))
    return reply_item_facts(item, parent, review=review)


def _author_facts(item: dict[str, Any], parent: dict[str, Any], kind: str, at: str,
                  shared: dict[str, Any]) -> dict[str, Any]:
    """Who wrote ``item``, who opened its ``kind`` (``pr`` or ``issue``) and how long after."""
    facts: dict[str, Any] = {}
    for key, value in (
        ("author_login", shared.get("author_login")),
        ("author_association", _optional_text(item, "author_association") and shared.get("author_association")),
        (f"{kind}_author_login", shared.get("pr_author_login")),
        (f"{kind}_author_association", _optional_text(parent, "author_association")),
        (f"minutes_since_{kind}_created", measurements.minutes_between(parent.get("created_at"), at)),
    ):
        if value is not None:
            facts[key] = value
    return facts


def _pr_facts(item: dict[str, Any], pr: dict[str, Any], at: str, shared: dict[str, Any]) -> dict[str, Any]:
    """The facts a review or a review comment shares, with the head the payload names."""
    facts = _author_facts(item, pr, "pr", at, shared)
    facts["has_reviewer_marker"] = "yes" if shared["has_reviewer_marker"] else "no"
    head = pr.get("head")
    if head is not None and (sha := _optional_text(shape.mapping(head), "sha")) is not None:
        facts["head_sha"] = sha
    if _optional_text(item, "commit_id") is not None:
        facts["commit_id"] = shared["commit_id"]
    return facts


def review_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One submitted or dismissed review."""
    action = payload["action"]
    review = shape.mapping(shape.required(payload, "review"))
    pr = shape.mapping(shape.required(payload, "pull_request"))
    number = shape.number(shape.required(pr, "number"))
    review_id = shape.number(shape.required(review, "id"))
    at = shape.timestamp(shape.required(review, "submitted_at") if action == "submitted"
                         else shape.required(pr, "updated_at"))
    shared = _shared(review, pr, review=True)
    attrs: dict[str, Any] = {
        "repo": repo, "pr_number": str(shared["pr_number"]), "review_id": str(shared["review_id"]),
        **_pr_facts(review, pr, at, shared),
    }
    if _optional_text(review, "state") is not None:
        attrs["state"] = shared["state"]
    return [OutcomeDraft(
        f"github:pr_review:{repo}:{review_id}:{action}", f"github.pr.review.{action}",
        at, pr_subject(repo, number), attrs,
    )]


def review_comment_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One created line comment on a pull request."""
    comment = shape.mapping(shape.required(payload, "comment"))
    pr = shape.mapping(shape.required(payload, "pull_request"))
    number = shape.number(shape.required(pr, "number"))
    comment_id = shape.number(shape.required(comment, "id"))
    at = shape.timestamp(shape.required(comment, "created_at"))
    shared = _shared(comment, pr, review=False)
    attrs: dict[str, Any] = {
        "repo": repo, "pr_number": str(shared["pr_number"]), "comment_id": str(shared["comment_id"]),
        **_pr_facts(comment, pr, at, shared),
    }
    if _optional_text(comment, "path") is not None:
        attrs["path"] = shared["path"]
    if comment.get("in_reply_to_id") is not None:
        shape.number(comment["in_reply_to_id"])
        attrs["in_reply_to_id"] = str(shared["in_reply_to_id"])
    return [OutcomeDraft(
        f"github:pr_review_comment:{repo}:{comment_id}:created", "github.pr.review_comment.created",
        at, pr_subject(repo, number), attrs,
    )]


def issue_comment_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One created conversation comment, on a pull request or on a plain issue."""
    comment = shape.mapping(shape.required(payload, "comment"))
    issue = shape.mapping(shape.required(payload, "issue"))
    number = shape.number(shape.required(issue, "number"))
    comment_id = shape.number(shape.required(comment, "id"))
    at = shape.timestamp(shape.required(comment, "created_at"))
    shared = _shared(comment, issue, review=False, pr=issue.get("pull_request") is not None)
    if issue.get("pull_request") is not None:
        attrs: dict[str, Any] = {
            "repo": repo, "pr_number": str(shared["pr_number"]), "comment_id": str(shared["comment_id"]),
            **_author_facts(comment, issue, "pr", at, shared),
            "has_reviewer_marker": "yes" if shared["has_reviewer_marker"] else "no",
        }
        return [OutcomeDraft(
            f"github:pr_comment:{repo}:{comment_id}:created", "github.pr.comment.created",
            at, pr_subject(repo, number), attrs,
        )]
    attrs = {
        "repo": repo, "issue_number": str(shared["pr_number"]), "comment_id": str(shared["comment_id"]),
        **_author_facts(comment, issue, "issue", at, shared),
    }
    return [OutcomeDraft(
        f"github:issue_comment:{repo}:{comment_id}:created", "github.issue.comment.created",
        at, f"github:issue:{repo}#{number}", attrs,
    )]
