"""GitHub lifecycle outcome facts; timestamps and identities come from payloads."""
from __future__ import annotations

from typing import Any

from events.connectors import github_outcome_input as shape
from events.connectors import github_outcome_measurements as measurements
from events.connectors.github_outcomes import OutcomeDraft, _issue_drafts, _pr_drafts, pr_subject


def pr_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    action = payload["action"]
    if action not in ("ready_for_review", "converted_to_draft"):
        return _pr_drafts(payload, repo)
    pr = shape.mapping(shape.required(payload, "pull_request"))
    number = shape.number(shape.required(pr, "number"))
    at = shape.timestamp(shape.required(pr, "updated_at"))
    subject = pr_subject(repo, number)
    name = "ready" if action == "ready_for_review" else "drafted"
    attrs: dict[str, Any] = {"repo": repo, "pr": str(number)}
    if name == "ready":
        minutes = measurements.minutes_between(pr.get("created_at"), at)
        if minutes is not None:
            attrs["minutes_since_pr_created"] = minutes
    return [OutcomeDraft(f"{subject}:{name}:{at}", f"github.pr.{name}", at, subject, attrs)]


def issue_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    if payload["action"] != "assigned":
        return _issue_drafts(payload, repo)
    issue = shape.mapping(shape.required(payload, "issue"))
    number = shape.number(shape.required(issue, "number"))
    at = shape.timestamp(shape.required(issue, "updated_at"))
    assignee = shape.mapping(shape.required(payload, "assignee"))
    login = shape.text(shape.required(assignee, "login"))
    subject = f"github:issue:{repo}#{number}"
    attrs: dict[str, Any] = {"repo": repo, "issue": str(number), "assignee_login": login}
    minutes = measurements.minutes_between(issue.get("created_at"), at)
    if minutes is not None:
        attrs["minutes_since_issue_created"] = minutes
    return [OutcomeDraft(f"{subject}:assigned:{login}:{at}", "github.issue.assigned", at, subject, attrs)]


def status_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    status_id = shape.number(shape.required(payload, "id"))
    sha = shape.text(shape.required(payload, "sha"))
    context = shape.text(shape.required(payload, "context"))
    state = shape.text(shape.required(payload, "state"))
    at = shape.timestamp(shape.required(payload, "updated_at"))
    subject = f"github:commit:{repo}:{sha}"
    attrs = {"repo": repo, "sha": sha, "context": context, "state": state}
    return [OutcomeDraft(f"github:status:{repo}:{status_id}:{at}", "github.status", at, subject, attrs)]


def release_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    release = shape.mapping(shape.required(payload, "release"))
    release_id = shape.number(shape.required(release, "id"))
    at = shape.timestamp(shape.required(release, "published_at"))
    tag = shape.text(shape.required(release, "tag_name"))
    attrs = {"repo": repo, "tag": tag,
             "prerelease": "yes" if shape.boolean(shape.required(release, "prerelease")) else "no",
             "draft": "yes" if shape.boolean(shape.required(release, "draft")) else "no"}
    subject = f"github:release:{repo}:{release_id}"
    return [OutcomeDraft(f"{subject}:published:{at}", "github.release.published", at, subject, attrs)]
