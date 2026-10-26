"""The one mapping from a GitHub webhook payload to outcome event drafts.

Pure: no database or network. ``map_github_outcomes`` turns one delivery into
zero or more ``OutcomeDraft`` rows for ``outcome_events`` (measurement spine
§3.2). A key names a fact, not a delivery: the same unchanged payload maps to the
same ``dedupe_key``, so re-admitting that payload is a no-op. That is exactly what
dedupe promises here, and nothing more.

The keys then split into two kinds, and a backfill has to know which is which. The
PR open, push, close and merge keys, the issue keys and the run id plus
``run_attempt`` in ``github.workflow_run.completed``, the job id in
``github.workflow_job.completed`` and the check run id plus ``completed_at`` in
``github.check_run.completed`` are built from identities the
repository still reports — a head or merge SHA, a close timestamp, a run's
``run_attempt`` — so a later read can reconstruct the same key. ``github.pr.reopened``,
``github.pr.retargeted`` and ``github.check_suite.completed`` are keyed on the
``updated_at`` the delivering payload reported: a fact about that delivery no later
read recovers, since the field moves with any subsequent edit or re-run. Re-deriving
those three from the API yields a different key and adds a row instead of deduping,
so backfilling them needs the original delivery. Reconstructing a key is also not a
promise that an event's other historical attributes still read the same way. The
repository name is lowercased once on entry and that one value is used in every key,
subject and relation.

These are raw facts. Nothing here says who owns a pull request: the author
login and any ``Owner thread ID:`` declaration are recorded as observed,
untrusted source facts, and attribution is derived later, by a reader that can
check them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from events.connectors.github_ownership import declared_owner_thread_ids
from events.connectors import github_outcome_input as shape
from events.connectors import github_outcome_measurements as measurements
from infra.outcome_event_attributes import ISSUE_KIND_VALUES, PRIORITY_VALUES

_INTRODUCED_BY = re.compile(r"^[ \t>*_-]*introduced[ \t]+by[ \t]+#(\d+)\b", re.I | re.M)
_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?:\s*:\s*|\s+)"
    r"((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+)",
    re.I,
)
_LANE_PREFIX = "lane:"


@dataclass(frozen=True)
class OutcomeDraft:
    """One ``outcome_events`` row before its write."""

    dedupe_key: str
    event_name: str
    ts: str
    subject_key: str
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = "github"


def pr_subject(repo: str, number: int | str) -> str:
    """The subject key every event about one pull request shares."""
    return f"github:pr:{repo}#{number}"


def declaration_attributes(pr: dict[str, Any]) -> dict[str, Any]:
    """Who opened a PR and what owner it declares, as observed and unverified.

    ``declared_owner_id`` is present only when the body declares exactly one
    owner; ``declaration_count`` says how many it declared. No title or body
    text is kept.
    """
    attrs: dict[str, Any] = {}
    author = pr.get("user")
    if author is not None:
        login = shape.text(shape.required(shape.mapping(author), "login"))
        attrs["author_login"] = login
    body = shape.body(pr.get("body"))
    declared = declared_owner_thread_ids(body)
    attrs["declaration_count"] = len(declared)
    if len(declared) == 1:
        attrs["declared_owner_id"] = next(iter(declared))
    return attrs


def introduced_by_numbers(body: str) -> list[int]:
    """Usable stated PR identities, in order; unusable optional mentions are absent."""
    numbers = []
    for text in _INTRODUCED_BY.findall(body or ""):
        try:
            numbers.append(shape.reference_number(text))
        except shape.PayloadRefusal:
            continue
    return list(dict.fromkeys(numbers))


def closes_issue_refs(body: str, default_repo: str) -> list[tuple[str, int]]:
    """(issue_repo, number) pairs from ``Closes/Fixes/Resolves [#repo/]#N`` mentions.

    A merged PR that says it closes something *states* a relation; whether the
    issue really closed is the event's ``confidence=stated`` limit, and PR
    cross-references cannot be told from issues without another read (rare by
    convention: the repo's Closes lines name issues).
    """
    pairs = []
    for ref in dict.fromkeys(m.lower() for m in _CLOSES.findall(body or "")):
        repo_part, _, number = ref.rpartition("#")
        try:
            parsed = shape.reference_number(number)
        except shape.PayloadRefusal:
            continue
        pairs.append((repo_part or default_repo.lower(), parsed))
    return list(dict.fromkeys(pairs))


def _pr_draft(
    key: str, subject: str, common: dict[str, Any], suffix: str, name: str, ts: str,
    **attrs: Any,
) -> OutcomeDraft:
    return OutcomeDraft(key + suffix, name, ts, subject, {**common, **attrs})


def _pr_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    action = payload.get("action")
    pr = shape.mapping(shape.required(payload, "pull_request"))
    number = shape.number(shape.required(pr, "number"))
    subject = pr_subject(repo, number)
    key = f"{subject}:"
    common = {
        "repo": repo, "pr": str(number),
        "base_ref": shape.text(shape.required(shape.mapping(shape.required(pr, "base")), "ref")),
        "head_sha": shape.text(shape.required(shape.mapping(shape.required(pr, "head")), "sha")),
    }

    if action == "opened":
        attrs = declaration_attributes(pr)
        if "draft" in pr:
            attrs["draft"] = "yes" if shape.boolean(pr["draft"]) else "no"
        return [_pr_draft(
            key, subject, common, "opened", "github.pr.opened",
            shape.timestamp(shape.required(pr, "created_at")),
            head_ref=shape.text(shape.required(shape.mapping(pr["head"]), "ref")), **attrs,
        )]
    if action == "synchronize":
        after = shape.text(payload["after"]) if "after" in payload else common["head_sha"]
        before = payload.get("before")
        extra = {"before_sha": shape.text(before)} if before is not None else {}
        return [_pr_draft(key, subject, common, f"head:{after}", "github.pr.pushed",
                          shape.timestamp(shape.required(pr, "updated_at")), **extra)]
    if action == "reopened":
        updated = shape.timestamp(shape.required(pr, "updated_at"))
        return [_pr_draft(key, subject, common, f"reopened:{updated}", "github.pr.reopened", updated)]
    if not shape.boolean(shape.required(pr, "merged")):
        closed = shape.timestamp(shape.required(pr, "closed_at"))
        return [_pr_draft(key, subject, common, f"closed:{closed}", "github.pr.closed", closed)]
    merged_at = shape.timestamp(shape.required(pr, "merged_at"))
    body = shape.body(pr.get("body"))
    minutes_since_opened = measurements.minutes_between(pr.get("created_at"), merged_at)
    revert = measurements.reverted_sha(pr.get("title"), body)
    revert_pr = measurements.reverted_pr_number(pr.get("title"), body, repo)
    measurement_attrs: dict[str, Any] = {}
    if minutes_since_opened is not None:
        measurement_attrs["minutes_since_opened"] = minutes_since_opened
    if revert is not None:
        measurement_attrs["reverts_sha"] = revert
    if revert_pr is not None:
        measurement_attrs["reverts_pr"] = revert_pr
    drafts = [_pr_draft(
        key, subject, common, "merged", "github.pr.merged", merged_at,
        head_ref=shape.text(shape.required(shape.mapping(pr["head"]), "ref")),
        merge_sha=shape.text(shape.required(pr, "merge_commit_sha")),
        **declaration_attributes(pr), **measurement_attrs,
    )]
    for introducing in introduced_by_numbers(body):
        drafts.append(OutcomeDraft(
            f"{key}introduced_by:{introducing}", "github.pr.introduced_by", merged_at,
            subject,
            {"repo": repo, "pr": str(number), "introduced_by_pr": str(introducing),
             "confidence": "stated"},
        ))
    for issue_repo, issue_number in closes_issue_refs(body, repo):
        drafts.append(OutcomeDraft(
            f"{key}closes:{issue_repo}#{issue_number}", "github.pr.closes_issue",
            merged_at, subject,
            {"repo": repo, "pr": str(number), "issue_repo": issue_repo,
             "issue": str(issue_number), "confidence": "stated"},
        ))
    return drafts


def _retarget_base_from(payload: dict[str, Any]) -> str | None:
    """Read a base change before consuming any PR/repository fact fields."""
    changes = shape.mapping(payload.get("changes", {}))
    if "base" not in changes:
        return None
    base_change = shape.mapping(changes["base"])
    if "ref" not in base_change:
        return None
    ref_change = shape.mapping(base_change["ref"])
    return shape.text(shape.required(ref_change, "from"))


def _retarget_drafts(payload: dict[str, Any], repo: str, base_from: str) -> list[OutcomeDraft]:
    """A base-branch change whose consumed source fields have been validated."""
    pr = shape.mapping(shape.required(payload, "pull_request"))
    number = shape.number(shape.required(pr, "number"))
    subject = pr_subject(repo, number)
    updated = shape.timestamp(shape.required(pr, "updated_at"))
    return [OutcomeDraft(
        f"{subject}:retargeted:{updated}", "github.pr.retargeted", updated, subject,
        {"repo": repo, "pr": str(number), "base_from": base_from,
         "base_ref": shape.text(shape.required(shape.mapping(shape.required(pr, "base")), "ref")),
         "head_sha": shape.text(shape.required(shape.mapping(shape.required(pr, "head")), "sha"))},
    )]


def _workflow_run_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """One completed Actions run: the spine's run identity is id + attempt.

    ``check_suite`` reuses one suite id across reruns and carries no attempt,
    so ``workflow_run.completed`` is the primary CI event; check_suite rows
    stay as the supplemental suite view.
    """
    run = shape.mapping(shape.required(payload, "workflow_run"))
    # An omitted run_attempt retains the compatibility default 1; it does not
    # establish whether the historical run was retried. Present values must be
    # positive integers because attempt is part of the run's dedupe identity.
    attempt = shape.number(run.get("run_attempt", 1))
    prs = shape.pr_numbers(run.get("pull_requests", []))
    run_id = shape.number(shape.required(run, "id"))
    head_sha = shape.text(shape.required(run, "head_sha"))
    updated = shape.timestamp(shape.required(run, "updated_at"))
    attrs: dict[str, Any] = {
        "repo": repo, "run_id": str(run_id), "attempt": attempt,
        "workflow_id": str(shape.number(shape.required(run, "workflow_id"))), "head_sha": head_sha,
        # `none` is the absent-key sentinel and nothing else. Truthiness defaulting
        # would rewrite a present null, empty string, False or 0 into a plausible
        # value; a defaulting get leaves the value as it arrived, so the store drops
        # and counts it instead.
        "conclusion": run.get("conclusion", "none"),
        "event": run.get("event", "none"), "pr_count": len(prs),
    }
    if run.get("head_branch") is not None:
        attrs["head_branch"] = shape.text(run["head_branch"])
    if len(prs) == 1:
        attrs["pr"] = str(prs[0])
    return [OutcomeDraft(
        f"github:workflow_run:{repo}:{run_id}:attempt:{attempt}",
        "github.workflow_run.completed", updated,
        f"github:commit:{repo}@{head_sha}", attrs,
    )]


def _check_suite_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    suite = shape.mapping(shape.required(payload, "check_suite"))
    prs = shape.pr_numbers(suite.get("pull_requests", []))
    suite_id = shape.number(shape.required(suite, "id"))
    head_sha = shape.text(shape.required(suite, "head_sha"))
    updated = shape.timestamp(shape.required(suite, "updated_at"))
    attrs: dict[str, Any] = {
        "repo": repo, "suite_id": str(suite_id), "head_sha": head_sha,
        "conclusion": suite.get("conclusion", "none"), "pr_count": len(prs),
    }
    if suite.get("head_branch") is not None:
        attrs["head_branch"] = shape.text(suite["head_branch"])
    if suite.get("app") is not None:
        app = shape.mapping(suite["app"])
        if app.get("slug") is not None:
            attrs["app"] = shape.text(app["slug"])
    if len(prs) == 1:
        attrs["pr"] = str(prs[0])
    return [OutcomeDraft(
        f"github:check_suite:{repo}:{suite_id}:completed:{updated}",
        "github.check_suite.completed", updated,
        f"github:commit:{repo}@{head_sha}", attrs,
    )]


def label_attributes(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Scalar label facts: highest priority, lane, kind, and the label count."""
    names = sorted({shape.text(shape.required(shape.mapping(label), "name"))
                    for label in shape.members(labels)})
    attrs: dict[str, Any] = {"label_count": len(names)}
    priority = next((p for p in PRIORITY_VALUES if p in names), None)
    if priority:
        attrs["priority"] = priority
    kind = next((k for k in ISSUE_KIND_VALUES if k in names), None)
    if kind:
        attrs["kind"] = kind
    lanes = [n[len(_LANE_PREFIX):] for n in names if n.startswith(_LANE_PREFIX)]
    if lanes:
        attrs["lane"] = lanes[0]
        attrs["lane_count"] = len(lanes)
    return attrs


def _issue_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    action = payload.get("action")
    issue = shape.mapping(shape.required(payload, "issue"))
    number = shape.number(shape.required(issue, "number"))
    subject = f"github:issue:{repo}#{number}"
    attrs = {"repo": repo, "issue": str(number)}
    if "labels" in issue:
        attrs.update(label_attributes(issue["labels"]))
    if action == "opened":
        return [OutcomeDraft(
            f"{subject}:opened", "github.issue.opened",
            shape.timestamp(shape.required(issue, "created_at")), subject, attrs,
        )]
    if issue.get("state_reason") is not None:
        attrs["state_reason"] = shape.text(issue["state_reason"])
    closed = shape.timestamp(shape.required(issue, "closed_at"))
    return [OutcomeDraft(
        f"{subject}:closed:{closed}", "github.issue.closed", closed, subject, attrs,
    )]


def _workflow_job_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    from events.connectors.github_ci_outcomes import workflow_job_drafts
    return workflow_job_drafts(payload, repo)


def _check_run_drafts(payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    from events.connectors.github_ci_outcomes import check_run_drafts
    return check_run_drafts(payload, repo)


# The public entry owns supported-action selection; private mappers consume it.
_MAPPERS = {
    "pull_request": (("opened", "synchronize", "reopened", "closed", "edited", "labeled", "unlabeled"), _pr_drafts),
    "check_suite": (("completed",), _check_suite_drafts),
    "workflow_run": (("completed",), _workflow_run_drafts),
    "workflow_job": (("completed",), _workflow_job_drafts),
    "check_run": (("completed",), _check_run_drafts),
    "issues": (("opened", "closed", "reopened", "labeled", "unlabeled"), _issue_drafts),
}
# Label changes and issue reopens map in github_label_outcomes, from the shared normalization.
_LABEL_ACTIONS = {"pull_request": ("labeled", "unlabeled"), "issues": ("reopened", "labeled", "unlabeled")}


def map_github_outcomes(event_type: str, payload: dict[str, Any]) -> list[OutcomeDraft]:
    """Return the outcome drafts one delivery carries; ``[]`` for anything unmapped.

    Raises ``PayloadRefusal`` only for consumed input fields that cannot describe
    a fact. Incidental exceptions are code faults, never input refusals.
    """
    dispatch = _MAPPERS.get(event_type)
    if dispatch is None:
        return []
    actions, mapper = dispatch
    payload = shape.mapping(payload)
    action = payload.get("action")
    if action not in actions:
        return []
    base_from = _retarget_base_from(payload) if event_type == "pull_request" and action == "edited" else None
    if event_type == "pull_request" and action == "edited" and base_from is None:
        return []
    repository = shape.mapping(shape.required(payload, "repository"))
    repo = shape.text(shape.required(repository, "full_name")).lower()
    if action in _LABEL_ACTIONS.get(event_type, ()):
        from events.connectors.github_label_outcomes import label_drafts
        return label_drafts(event_type, payload, repo)
    if base_from is not None:
        return _retarget_drafts(payload, repo, base_from)
    return mapper(payload, repo)
