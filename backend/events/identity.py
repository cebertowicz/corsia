"""Event payload to registry routing identity."""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE

ISSUE_TYPE = "issue"

log = logging.getLogger(__name__)


class RoutingIdentity(NamedTuple):
    """The registry ``(integration, type, external_id)`` an event routes to."""

    integration: str
    type: str
    external_id: str


class PrResolutionError(RuntimeError):
    """A check_suite PR fallback lookup failed and must not be treated as no-PR."""


def pr_external_id(repo: str, pr_number: int | str) -> str:
    """GitHub PR external id; issues share the same ``<owner>/<repo>#<number>`` shape."""
    return f"{repo}#{pr_number}"


def _pr_number_from_payload(payload: dict[str, Any]) -> int | None:
    """Extract the PR number from any PR-shaped GitHub payload."""
    pr = payload.get("pull_request") or {}
    if pr.get("number"):
        return int(pr["number"])
    issue = payload.get("issue") or {}
    if issue.get("number") and issue.get("pull_request"):
        return int(issue["number"])
    return None


CHECK_SUITE_COMPLETED_EVENTS = frozenset(
    {
        "webhook.github.check_suite_completed",
    }
)


def _check_suite_pr_metadata(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize check_suite pull_request entries to the SHA fallback shape."""
    normalized: list[dict[str, Any]] = []
    for pr in prs:
        raw_head = pr.get("head")
        head = raw_head if isinstance(raw_head, dict) else {}
        raw_base = pr.get("base")
        base = raw_base if isinstance(raw_base, dict) else {}
        number_value = pr.get("number")
        if number_value is None:
            continue
        try:
            number = int(number_value)
        except (TypeError, ValueError):
            continue
        normalized.append(
            {
                "number": number,
                "state": str(pr.get("state") or ""),
                "base_ref": str(base.get("ref") or ""),
                "head_ref": str(head.get("ref") or ""),
                "head_sha": str(head.get("sha") or ""),
            }
        )
    return normalized


def _single_current_pr(matches: list[dict[str, Any]], *, ambiguity: str) -> dict[str, Any]:
    """Return one current PR, preferring the live/open PR when history also matches."""
    if len(matches) == 1:
        return matches[0]
    open_matches = [pr for pr in matches if pr.get("state") == "open"]
    if len(open_matches) == 1:
        return open_matches[0]
    raise PrResolutionError(ambiguity)


def _select_pr_metadata_for_check_suite(
    prs: list[dict[str, Any]], *, head_sha: str, head_branch: str | None
) -> dict[str, Any] | None:
    """Select the PR metadata whose current head matches the check_suite head."""
    sha_matches = [pr for pr in prs if pr.get("head_sha") == head_sha]
    if not sha_matches:
        return None
    if head_branch:
        branch_matches = [pr for pr in sha_matches if pr.get("head_ref") == head_branch]
        if not branch_matches:
            raise PrResolutionError(
                f"no PR matched head branch {head_branch!r} for {head_sha[:8]}"
            )
        return _single_current_pr(
            branch_matches,
            ambiguity=(
                f"multiple PRs matched head branch {head_branch!r} for {head_sha[:8]}"
            ),
        )
    return _single_current_pr(
        sha_matches,
        ambiguity=f"multiple PRs matched {head_sha[:8]}",
    )


def check_suite_pr_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the payload-local PR metadata selected for this check_suite head."""
    cs = payload.get("check_suite") or {}
    prs = cs.get("pull_requests") or []
    head_sha = cs.get("head_sha")
    head_branch = cs.get("head_branch")
    if not prs:
        return None
    normalized = _check_suite_pr_metadata(prs)
    if not normalized:
        return None
    payload_has_head_sha = any(pr.get("head_sha") for pr in normalized)
    if isinstance(head_sha, str) and head_sha:
        if not payload_has_head_sha:
            return None
        selected = _select_pr_metadata_for_check_suite(
            normalized,
            head_sha=head_sha,
            head_branch=head_branch if isinstance(head_branch, str) else None,
        )
        if selected is None and len(normalized) > 1:
            raise PrResolutionError("multiple check_suite PRs did not match suite head")
        return selected
    if len(normalized) == 1:
        return normalized[0]
    raise PrResolutionError("multiple check_suite PRs lacked suite head metadata")


def pr_number_from_check_suite_payload(payload: dict[str, Any]) -> int | None:
    """Resolve a check_suite PR number using only payload-local PR entries."""
    selected = check_suite_pr_metadata_from_payload(payload)
    return int(selected["number"]) if selected is not None else None


def check_suite_pr_metadata_from_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return selected check_suite PR metadata, using the head-SHA fallback.

    Shared by normalization, routing, and reviewer gate identity. A check_suite
    event's PR identity and branch facts must come from one selected PR, not a
    payload-local path in one caller and a head-SHA fallback in another.

    A payload-selected PR that lacks ``base_ref`` (GitHub sends partial
    ``pull_requests`` entries) is enriched via the head-SHA lookup — routing
    classifies terminal ``main`` failures off ``base_ref``, so returning a
    branch-factless selection would misclassify them as generic noise.
    """
    selected = check_suite_pr_metadata_from_payload(payload)
    if selected is not None and selected.get("base_ref"):
        return selected

    cs = payload.get("check_suite") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    head_sha = cs.get("head_sha")
    head_branch = cs.get("head_branch")
    if not repo or not head_sha:
        return selected

    from reviewer.gh_helpers import get_prs_for_sha

    prs = get_prs_for_sha(repo, head_sha)
    if selected is not None:
        # Identity is payload-locked — the lookup only backfills the missing
        # branch facts for that SAME PR. A lookup failure degrades loudly to
        # the branch-factless selection (the event still routes to its owner)
        # instead of dead-lettering an identity-known event.
        if prs is None:
            log.warning(
                "check_suite PR #%s selected from payload but base_ref "
                "enrichment failed for %s@%s — main-failure classification "
                "may miss", selected["number"], repo, str(head_sha)[:8],
            )
            return selected
        match = next(
            (pr for pr in prs
             if str(pr.get("number")) == str(selected["number"])),
            None,
        )
        if match is not None and match.get("base_ref"):
            enriched = dict(selected)
            enriched["base_ref"] = str(match["base_ref"])
            if not enriched.get("head_ref") and match.get("head_ref"):
                enriched["head_ref"] = str(match["head_ref"])
            return enriched
        log.warning(
            "check_suite PR #%s: base_ref enrichment MISS for %s@%s (PR "
            "absent from the head-SHA listing or still branchless) — the "
            "router treats the unknown base conservatively",
            selected["number"], repo, str(head_sha)[:8],
        )
        return selected

    if prs is None:
        raise PrResolutionError(f"failed to resolve PRs for {repo}@{head_sha[:8]}")
    if not prs:
        return None
    return _select_pr_metadata_for_check_suite(
        prs,
        head_sha=head_sha,
        head_branch=head_branch if isinstance(head_branch, str) else None,
    )


def pr_number_from_check_suite_event(payload: dict[str, Any]) -> int | None:
    """Resolve the PR number for a check_suite event."""
    selected = check_suite_pr_metadata_from_event(payload)
    return int(selected["number"]) if selected is not None else None


def resolve_identity(
    internal_event: str, payload: dict[str, Any]
) -> RoutingIdentity | None:
    """Map an internal event + payload to its registry routing identity."""
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None

    if internal_event.startswith("webhook.github."):
        if internal_event in CHECK_SUITE_COMPLETED_EVENTS:
            pr_number = pr_number_from_check_suite_event(payload)
        else:
            pr_number = _pr_number_from_payload(payload)
        if pr_number is None:
            return None
        return RoutingIdentity(
            GITHUB_INTEGRATION, PR_TYPE, pr_external_id(repo, pr_number)
        )

    return None
