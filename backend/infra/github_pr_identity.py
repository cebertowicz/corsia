"""Canonical GitHub pull-request artifact identity.

The single source of truth for the PR artifact ``type`` + ``external_id``
shape. Shared by the registration boundary (``tools.artifacts`` /
``registry.models``) and routing (``events.identity``) so both use the same
identity.

Lives in ``infra`` because both consumers depend on that layer.
"""

from __future__ import annotations

GITHUB_INTEGRATION = "github"
PR_TYPE = "pull-request"

# Historically-observed drifted spellings that must canonicalize to PR_TYPE.
PR_TYPE_ALIASES = frozenset({"pull_request", "pr", "github_pr"})


def canonicalize_github_pr_type(type_: str) -> str:
    """Map a known GitHub PR-type alias to the canonical ``PR_TYPE``.

    Passes through unchanged if ``type_`` isn't a recognized alias (e.g. a
    non-PR github artifact type).
    """
    return PR_TYPE if (type_ == PR_TYPE or type_ in PR_TYPE_ALIASES) else type_


def validate_github_pr_external_id(external_id: str) -> str | None:
    """Return an error message if ``external_id`` isn't ``<owner>/<repo>#<N>``.

    Returns ``None`` when valid. The register boundary doesn't reliably know
    the repo, so a bare PR number must be rejected loudly rather than stored
    (silently storing it would miss the routing lookup, same as the bug this
    guards against).
    """
    owner_repo, _, number = external_id.rpartition("#")
    if owner_repo.count("/") == 1 and number.isdigit() and "/" not in number:
        owner, _, repo = owner_repo.partition("/")
        if owner and repo:
            return None
    return (
        "github pull-request external_id must be '<owner>/<repo>#<N>' "
        f"(e.g. cebertowicz/corsia#123), got '{external_id}'"
    )
