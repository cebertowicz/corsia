"""GitHub connector manifest (declared data).

Reuses the single GitHub artifact-id format source — ``events.identity`` —
so there is no parallel derivation of a PR's identity (replace-don't-preserve):
the legacy router and the new envelope path resolve a PR to the *same*
``repo#N`` external id.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from events.connectors import (
    ConnectorManifest,
    EventTypeDecl,
    ObservedArtifactDecl,
    ShippedObserveRuleDecl,
    register,
)
if TYPE_CHECKING:
    from events.envelope import EventEnvelope
from events.identity import (
    GITHUB_INTEGRATION,
    PR_TYPE,
    RoutingIdentity,
    check_suite_pr_metadata_from_event,
    pr_external_id,
)
from events.connectors.github_label_events import IDENTITY, change_event
from events.connectors.github_ownership import declaration_facts
from events.connectors.github_content_facts import content_facts, content_kind, repository_name
from events.connectors.github_receiver import GITHUB_RECEIVER
from events.identity_formula import MalformedEventError, RelationshipDecl
from infra.webhook_delivery_log import comment_object_key

_CHECK_TO_PR = RelationshipDecl("check_suite", "check_to_pull_request", "pull-request")

PrEnd = Literal["merged", "closed"]
"""How a closed pull request ended — the whole vocabulary of a PR terminal state."""

PR_MERGED_KEY = "merged"
"""The one GitHub field that decides a PR's end. Named here so no consumer
spells it out again; a rename moves the rule, the classifier and the tests."""

# The connector owns the names its manifest declares for routed GitHub events.
# The webhook mapper (events/github_mapper.py) imports these values when it maps
# payloads, so a declaration cannot silently name an event the mapper never emits.
GITHUB_ROUTED_EVENTS = {
    "pr.opened": "webhook.github.pr_opened",
    "pr.synchronize": "webhook.github.pr_synchronize",
    "pr.ready_for_review": "webhook.github.pr_ready_for_review",
    "pr.labeled": "webhook.github.pr_labeled",
    "pr.merged": "webhook.github.pr_merged",
    "pr.closed": "webhook.github.pr_closed",
    "check.completed": "webhook.github.check_suite_completed",
    "issue.opened": "webhook.github.issue_opened",
    "issue_comment.created": "webhook.github.issue_comment",
    "pr_comment.created": "webhook.github.pr_comment",
}

# Each consumer maps PrEnd into its own vocabulary; the rule itself is never
# re-written.
_PR_END_TYPES: dict[PrEnd, str] = {"merged": "pr.merged", "closed": "pr.closed"}

#: The ``resolved`` key for the pull request routing selected for a check suite.
CHECK_SUITE_PR = "check_suite_pr"


def _json_object(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """The object GitHub sends at ``key``, or empty when the body holds anything else.

    A webhook body is GitHub's today and whoever controls it's tomorrow: a stored,
    replayed or forged delivery can put a list, string or number where an object is
    expected. Reading a fact out of that answers "not there" — it never raises out
    of the rule, which would turn one malformed delivery into a failed consumer.
    """
    value = payload.get(key)
    return value if isinstance(value, Mapping) else {}


def pr_end_state(payload: Mapping[str, Any]) -> PrEnd | None:
    """The one PR terminal rule: how a ``closed`` pull_request webhook ended.

    A closed pull request ended ``"merged"`` when ``pull_request.merged`` is
    True and ``"closed"`` when it is False; ``None`` means the payload is not a
    classified close — a different action, or a close whose ``merged`` fact is
    missing, null, any non-bool, or has no ``pull_request`` object to read. Each
    caller decides what that means: the webhook mapper declines to name it, this
    connector's classifier refuses to guess it, the router refuses to write it and
    says so.

    Replaces the copies this rule kept in ``events.github_mapper`` and
    ``events.router`` (#2381 review, EAS2). The one-time REST reconciliation in
    ``registry/pr_history_reconciliation.py`` reads GitHub's REST shape
    (``state``/``merged_at``), not this webhook shape, and stays separate.
    """
    if payload.get("action") != "closed":
        return None
    merged = _json_object(payload, "pull_request").get(PR_MERGED_KEY)
    if merged is True:
        return "merged"
    if merged is False:
        return "closed"
    return None


_PR_URL_RE = re.compile(
    r"https://github\.com/([^/\s?#]+/[^/\s?#]+)/pull/([1-9]\d*)"
)
_FAILED_EXIT_RE = re.compile(r"^Exit code: [1-9]\d*$")


def _pr_artifact_in_output(result_text: str) -> ObservedArtifactDecl | None:
    """Return the one canonical PR URL named by a successful command's output."""
    lines = [line.strip() for line in result_text.splitlines() if line.strip()]
    if any(_FAILED_EXIT_RE.fullmatch(line) for line in lines):
        return None
    matches = [
        match
        for line in lines
        if (match := _PR_URL_RE.fullmatch(line)) is not None
    ]
    if len(matches) != 1:
        return None
    repo, number_text = matches[0].groups()
    number = int(number_text)
    return ObservedArtifactDecl(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id(repo, number),
        title=f"PR #{number}",
        path=f"https://github.com/{repo}/pull/{number}",
    )


def _pr_artifact_in_result(result_text: str) -> ObservedArtifactDecl | None:
    """Read a GitHub MCP created object's URL field, or the shell's PR URL line."""
    try:
        result, _ = json.JSONDecoder().raw_decode(result_text.lstrip())
    except (TypeError, ValueError):
        return _pr_artifact_in_output(result_text)
    if not isinstance(result, dict):
        return None
    item = result.get("comment", result)
    if not isinstance(item, dict):
        return None
    url, object_id = item.get("url"), item.get("id")
    if not isinstance(url, str) or not isinstance(object_id, str) or not object_id.isdigit():
        return None
    match = re.fullmatch(_PR_URL_RE.pattern + r"(?:#issuecomment-([1-9]\d*))?", url)
    if match is None:
        return None
    repo, number, comment_id = match.groups()
    if ("comment" in result and comment_id is None) or (comment_id is not None and comment_id != object_id):
        return None
    return ObservedArtifactDecl(
        integration=GITHUB_INTEGRATION, type=PR_TYPE,
        external_id=pr_external_id(repo, number), title=f"PR #{number}",
        created_event_ids=(comment_object_key("issue_comment", comment_id),) if comment_id else (),
    )


def _classify(raw: dict[str, Any]) -> str:
    if raw.get("check_suite"):
        return "check.completed"
    action = raw.get("action", "")
    if kind := content_kind(raw):
        return kind
    if raw.get("issue") and not raw.get("pull_request") and f"issue.{action}" in IDENTITY:
        return f"issue.{action}"
    if action == "closed":
        end = pr_end_state(raw)
        if end is None:
            # Fail closed: guessing a class for a close we cannot read would
            # register a terminal event GitHub never sent.
            pr = _json_object(raw, "pull_request")
            raise MalformedEventError(
                "github: a `closed` pull_request needs a boolean "
                f"`pull_request.{PR_MERGED_KEY}` to classify (got "
                f"{type(pr.get(PR_MERGED_KEY)).__name__} for "
                f"{_json_object(raw, 'repository').get('full_name')}#{pr.get('number')})"
            )
        return _PR_END_TYPES[end]
    if action == "synchronize":
        return "pr.synchronize"
    if action == "ready_for_review":
        return "pr.ready_for_review"
    if action in ("labeled", "unlabeled"):
        return f"pr.{action}"
    return "pr.opened"


def _extract_facts(raw: dict[str, Any], resolved: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A webhook's facts. A check suite's pull request is the one routing selected
    (``resolved[CHECK_SUITE_PR]``), looked up here only when none was passed."""
    facts: dict[str, Any] = {"repo": repository_name(raw)}
    pr = raw.get("pull_request") or {}
    content = content_facts(raw) if any(raw.get(k) for k in ("pull_request", "issue", "comment", "review")) else {}
    if pr:
        user = _json_object(pr, "user")
        facts.update({
            "pr_number": pr.get("number"), "action": raw.get("action"),
            "merged": pr.get("merged"),
            "branch": (pr.get("base") or {}).get("ref"),
            "sha": (pr.get("head") or {}).get("sha"), "author": user.get("login"),
        })
    kind = "pull_request" if pr else "issues" if raw.get("issue") else None
    change = change_event(kind, raw) if kind else None
    facts.update(change.facts if change else {})
    facts.update(content)
    if (pr and not raw.get("comment") and not raw.get("review")
            and content.get("author_origin") == "inside" and pr.get("title") is not None):
        facts["title"] = pr["title"]
    cs = raw.get("check_suite") or {}
    if cs:
        selected = (resolved[CHECK_SUITE_PR] if resolved is not None and CHECK_SUITE_PR in resolved
                    else check_suite_pr_metadata_from_event(raw))
        facts.update(_check_suite_facts(cs, selected))
    return {k: v for k, v in facts.items() if v is not None}


def _check_suite_facts(cs: Mapping[str, Any], selected_pr: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "check_suite_id": cs.get("id"), "conclusion": cs.get("conclusion"),
        "head_sha": cs.get("head_sha"),
        "pr_number": selected_pr.get("number") if selected_pr else None,
        "branch": selected_pr.get("base_ref") if selected_pr else None,
    }


def _artifact_ref(event_type: str, facts: dict, raw: dict) -> dict | None:
    number = facts.get("pr_number")
    if number is None:
        return None
    repo = facts.get("repo")
    return {"integration": GITHUB_INTEGRATION, "type": PR_TYPE,
            "external_id": pr_external_id(repo, number)}


def _relationships(event_type: str, facts: dict, raw: dict) -> tuple[dict, ...]:
    if event_type != "check.completed":
        return ()
    number = facts.get("pr_number")
    if number is None:
        return ()
    return ({"edge": _CHECK_TO_PR.edge, "to_type": _CHECK_TO_PR.to_type,
             "to_ref": pr_external_id(facts.get("repo"), number)},)


def _project_repositories(project_id: str) -> frozenset[str] | None:
    """The repositories a project's registered source repositories track."""
    from events.connectors.github_notifications_feed import FeedError, tracked_github_repositories

    try:
        return tracked_github_repositories(project_id)
    except FeedError:
        return None


def _repository_projects(repository: str) -> frozenset[str] | None:
    """The projects whose registered source repositories track ``repository``."""
    from events.connectors.github_notifications_feed import (
        FeedError,
        projects_tracking_github_repository,
    )

    try:
        return projects_tracking_github_repository(repository)
    except FeedError:
        return None


_PR_FILTERABLE = ("repo", "branch", "sha", "pr_number", "author", "author_origin", "item_version")
# A label change adds the one name changed, the item's names after it, who did it and when.
_PR_LABEL_FILTERABLE = (*_PR_FILTERABLE, "label", "labels", "actor", "at")
_ISSUE_FILTERABLE = ("repo", "issue_number", "label", "labels", "actor", "at",
                     "minutes_since_opened", "state_reason", "author", "author_origin", "item_version")
_CONTENT_FILTERABLE = ("repo", "author", "author_origin", "item_version")
_HEAD_CHECKS_FILTERABLE = ("repo", "head_sha", "head_branch", "pr_number", "conclusion")
_CHECK_FILTERABLE = ("repo", "branch", "head_sha", "check_suite_id", "conclusion")

MANIFEST = register(ConnectorManifest(
    connector="github",
    namespace="github",
    version="1",
    event_types={
        "pr.opened": EventTypeDecl(
            "pr.opened", ("repo", "pr_number", "action"), _PR_FILTERABLE,
            artifact_type=PR_TYPE, artifact_integration=GITHUB_INTEGRATION,
            event_class="actionable", routed_event=GITHUB_ROUTED_EVENTS["pr.opened"]),
        "pr.synchronize": EventTypeDecl(
            "pr.synchronize", ("repo", "pr_number", "action", "sha"), _PR_FILTERABLE,
            artifact_type=PR_TYPE, artifact_integration=GITHUB_INTEGRATION,
            event_class="actionable", routed_event=GITHUB_ROUTED_EVENTS["pr.synchronize"]),
        "pr.ready_for_review": EventTypeDecl(
            "pr.ready_for_review", ("repo", "pr_number", "action", "sha"),
            _PR_FILTERABLE, artifact_type=PR_TYPE,
            artifact_integration=GITHUB_INTEGRATION, event_class="informational",
            routed_event=GITHUB_ROUTED_EVENTS["pr.ready_for_review"]),
        "pr.labeled": EventTypeDecl(
            "pr.labeled", IDENTITY["pr.labeled"],
            _PR_LABEL_FILTERABLE, artifact_type=PR_TYPE,
            artifact_integration=GITHUB_INTEGRATION, event_class="informational",
            routed_event=GITHUB_ROUTED_EVENTS["pr.labeled"]),
        "pr.unlabeled": EventTypeDecl(
            "pr.unlabeled", IDENTITY["pr.unlabeled"], _PR_LABEL_FILTERABLE,
            artifact_type=PR_TYPE, artifact_integration=GITHUB_INTEGRATION),
        **{suffix: EventTypeDecl(suffix, facts, _ISSUE_FILTERABLE,
                                 routed_event=GITHUB_ROUTED_EVENTS.get(suffix))
           for suffix, facts in IDENTITY.items() if suffix.startswith("issue.")},
        **{name: EventTypeDecl(name, ("repo", "item_version"), _CONTENT_FILTERABLE,
                                routed_event=GITHUB_ROUTED_EVENTS.get(name))
           for name in ("issue.edited", "issue_comment.created", "issue_comment.edited",
                        "pr_comment.created", "pr_comment.edited", "review.submitted", "review.edited")},
        "head.checks_completed": EventTypeDecl(
            "head.checks_completed", ("repo", "head_sha"), _HEAD_CHECKS_FILTERABLE,
            artifact_type=PR_TYPE, artifact_integration=GITHUB_INTEGRATION),
        "pr.merged": EventTypeDecl(
            "pr.merged", ("repo", "pr_number", "action"), _PR_FILTERABLE,
            artifact_type=PR_TYPE, artifact_integration=GITHUB_INTEGRATION,
            event_class="informational", routed_event=GITHUB_ROUTED_EVENTS["pr.merged"]),
        "pr.closed": EventTypeDecl(
            "pr.closed", ("repo", "pr_number", "action"), _PR_FILTERABLE,
            artifact_type=PR_TYPE, artifact_integration=GITHUB_INTEGRATION,
            event_class="informational", routed_event=GITHUB_ROUTED_EVENTS["pr.closed"]),
        "check.completed": EventTypeDecl(
            "check.completed",
            ("repo", "check_suite_id", "conclusion", "head_sha"),
            _CHECK_FILTERABLE, artifact_type=PR_TYPE,
            artifact_integration=GITHUB_INTEGRATION,
            relationships=(_CHECK_TO_PR,), event_class="actionable",
            routed_event=GITHUB_ROUTED_EVENTS["check.completed"]),
    },
    classify=_classify,
    extract_facts=_extract_facts,
    build_artifact_ref=_artifact_ref,
    artifact_extractors={PR_TYPE: _pr_artifact_in_result},
    app_identities=frozenset({"https://api.githubcopilot.com", "ghcr.io/github/github-mcp-server"}),
    shipped_observe_rules=(
        ShippedObserveRuleDecl(
            id="ruleset_github_pr_create", tool="bash", argv_prefix=("gh", "pr", "create"), item_type=PR_TYPE,
        ),
        ShippedObserveRuleDecl(
            id="ruleset_github_mcp_pr_create", tool="create_pull_request", argv_prefix=(), item_type=PR_TYPE,
        ),
    ),
    resolve_relationships=_relationships,
    repository_fact="repo",
    project_repositories=_project_repositories,
    repository_projects=_repository_projects,
    receiver=GITHUB_RECEIVER,
))


def github_event_class(payload: dict[str, Any]) -> str:
    """Read the connector manifest's declared class for one payload."""
    return MANIFEST.decl(MANIFEST.classify(payload)).event_class


def github_routing_envelope(
    internal_event: str,
    identity: RoutingIdentity | None,
    payload: dict[str, Any],
) -> EventEnvelope:
    """Wrap a resolved GitHub identity for generic owner resolution."""
    from events.envelope import EventEnvelope

    artifact_ref = None
    if identity is not None:
        artifact_ref = {
            "integration": identity.integration,
            "type": identity.type,
            "external_id": identity.external_id,
        }
    routed_type = MANIFEST.routed_type(internal_event)
    return EventEnvelope(
        event_id="",
        type=internal_event,
        connector=None,
        facts=declaration_facts(payload),
        relationships=(),
        artifact_ref=artifact_ref,
        provenance={},
        event_class=MANIFEST.decl(routed_type).event_class if routed_type else github_event_class(payload),
        ownerless_ok=False,
        origin="connector",
    )


def identity_from_check_pr_metadata(
    payload: dict[str, Any],
    selected_pr: dict[str, Any] | None,
) -> RoutingIdentity | None:
    """Build routing identity from already-selected check-suite PR data."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo or selected_pr is None:
        return None
    return RoutingIdentity(GITHUB_INTEGRATION, PR_TYPE, pr_external_id(repo, selected_pr["number"]))
