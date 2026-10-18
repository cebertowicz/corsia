"""Small GitHub CLI helpers for reviewer-gate checks."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

from reviewer.gh_api import run_gh

log = logging.getLogger(__name__)

_GH_TIMEOUT_S = 30.0
STATUS_STAMP_INSTANCE_CHARS = 12
STATUS_WRITE_ID_CHARS = 12
STATUS_WRITE_STAMP = re.compile(
    rf" \[m:[0-9a-f]{{{STATUS_STAMP_INSTANCE_CHARS}}} "
    rf"w:[0-9a-f]{{{STATUS_WRITE_ID_CHARS}}}\]$",
)



def _run_gh(
    args: list[str],
    timeout: float = _GH_TIMEOUT_S,
) -> str | None:
    """Run a ``gh`` CLI command and return stdout, or None on failure."""
    try:
        result = run_gh(args, timeout=timeout)
    except FileNotFoundError:
        log.warning("gh CLI not on PATH")
        return None
    except subprocess.TimeoutExpired:
        log.warning("gh %s timed out after %.1fs", args[:3], timeout)
        return None
    except OSError as exc:
        log.warning("gh invocation failed: %s", exc)
        return None

    if result.returncode != 0:
        log.warning(
            "gh %s exited %d: %s",
            args[:3],
            result.returncode,
            result.stderr[:200],
        )
        return None

    return result.stdout


def get_check_runs_for_sha(repo: str, head_sha: str) -> list[dict[str, str]] | None:
    """Fetch all check runs for a commit SHA via the REST API (``gh api``).

    Uses ``--paginate`` + ``--jq`` to retrieve every page (avoids the default
    30-run cap), and the REST/core quota (separate 5000/hr pool from GraphQL)
    so it stays available when GraphQL is exhausted — the root cause of BUG
    #574/#608.

    ``--paginate --jq '.check_runs[]'`` emits one JSON object per check run
    across all pages on separate stdout lines.  An empty response (no check
    runs yet) returns ``[]``; a fetch/parse error returns ``None``.

    Returns one ``{name, state}`` dict per check name, selecting the newest
    run by ``completed_at``, falling back to ``started_at`` and then the run
    id. ``state`` is the uppercase GitHub conclusion (``SUCCESS``,
    ``FAILURE``, ``NEUTRAL``, ``SKIPPED``, …) or the in-progress status
    (``IN_PROGRESS``, ``QUEUED``) when ``conclusion`` is null (not yet set by
    GitHub).
    """
    # 60s covers paginated fetches with realistic network latency.
    out = _run_gh(
        [
            "api",
            f"repos/{repo}/commits/{head_sha}/check-runs",
            "--paginate",
            "--jq",
            ".check_runs[]",
        ],
        timeout=60.0,
    )
    if out is None:
        return None  # command failed (rate-limit, auth error, etc.)
    if not out.strip():
        return []  # commit has no check runs yet
    runs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            log.warning(
                "Failed to parse check-run entry for %s@%s: %r",
                repo,
                head_sha[:8],
                line[:80],
            )
            return None
        if isinstance(r, dict):
            runs.append(r)

    newest_runs: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name") or "")
        previous = newest_runs.get(name)
        if previous is None or _check_run_recency(run) > _check_run_recency(previous):
            newest_runs[name] = run

    return [
        {
            "name": str(run.get("name") or ""),
            # conclusion is null while in-progress; fall back to status
            "state": (run.get("conclusion") or run.get("status") or "").upper(),
        }
        for run in newest_runs.values()
    ]


def _check_run_recency(run: dict[str, Any]) -> tuple[str, int]:
    """Return the GitHub ordering fields that select a check run's latest attempt."""
    timestamp = str(run.get("completed_at") or run.get("started_at") or "")
    try:
        run_id = int(run.get("id") or 0)
    except (TypeError, ValueError):
        run_id = 0
    return timestamp, run_id


def get_commit_status_for_context(
    repo: str, sha: str, context: str,
) -> dict[str, str] | None:
    """Fetch the latest commit status for one status context via the REST API.

    ``GET /repos/{repo}/commits/{sha}/status`` (the combined-status endpoint)
    returns, per context, only the NEWEST status — GitHub collapses older
    posts — so the matched entry is the current authoritative state of
    ``context`` regardless of how many writers posted before. Uses
    ``--paginate`` because the ``statuses`` array pages at 100 contexts.
    Like the sibling helpers, it spends the REST/core quota (separate from
    GraphQL) so the read stays available when GraphQL is exhausted.

    Returns ``{"state", "description"}`` for the newest status on ``context``,
    ``{}`` when that context has no status yet, or ``None`` on a fetch/parse
    error (callers own their fail-open/fail-closed policy).
    """
    out = _run_gh(
        [
            "api",
            f"repos/{repo}/commits/{sha}/status",
            "--paginate",
            "--jq",
            ".statuses[]",
        ],
        timeout=60.0,
    )
    if out is None:
        return None  # command failed (rate-limit, auth error, etc.)
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            status = json.loads(line)
        except json.JSONDecodeError:
            log.warning(
                "Failed to parse commit-status entry for %s@%s: %r",
                repo, sha[:8], line[:80],
            )
            return None
        if isinstance(status, dict) and status.get("context") == context:
            state = str(status.get("state") or "")
            description = str(status.get("description") or "")
            if not STATUS_WRITE_STAMP.search(description):
                log.warning(
                    "reviewer-status: un-attributed standing status for %s@%s "
                    "state=%s; honouring its existing GitHub state",
                    repo,
                    sha[:8],
                    state,
                )
            return {"state": state, "description": description}
    return {}  # sha has no status on this context yet


def get_prs_for_sha(repo: str, head_sha: str) -> list[dict[str, Any]] | None:
    """Return PR metadata for PRs a commit SHA belongs to, via the REST API.

    The reliable fallback for check_suite routing: GitHub delivers
    ``check_suite.pull_requests`` EMPTY intermittently (and always cross-fork),
    which strands a green PR because the owner-wake + reviewer gate can't
    resolve the PR number. ``GET /repos/{repo}/commits/{sha}/pulls`` always
    resolves it. Uses the REST/core quota (separate from GraphQL) for the same
    quota-resilience reason as the sibling check helpers.

    Returns compact PR metadata (``[]`` when the commit has no associated PR),
    or ``None`` on a fetch/parse error (never a silent empty-as-success).
    """
    out = _run_gh(["api", f"repos/{repo}/commits/{head_sha}/pulls"], timeout=30.0)
    if out is None:
        return None  # command failed (rate-limit, auth error, etc.)
    if not out.strip():
        return []  # commit belongs to no PR
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        log.warning("Failed to parse PR list for %s@%s", repo, head_sha[:8])
        return None
    if not isinstance(data, list):
        log.warning("Unexpected PR list JSON for %s@%s", repo, head_sha[:8])
        return None

    prs: list[dict[str, Any]] = []
    for pr in data:
        if not isinstance(pr, dict):
            log.warning("Unexpected PR entry for %s@%s: %r", repo, head_sha[:8], pr)
            return None
        base = pr.get("base")
        if not isinstance(base, dict):
            base = {}
        head = pr.get("head")
        if not isinstance(head, dict):
            head = {}
        number_value = pr.get("number")
        if number_value is None:
            log.warning("PR entry missing number for %s@%s", repo, head_sha[:8])
            return None
        try:
            number = int(number_value)
        except (TypeError, ValueError):
            log.warning(
                "Failed to parse PR number for %s@%s: %r",
                repo,
                head_sha[:8],
                number_value,
            )
            return None
        prs.append(
            {
                "number": number,
                "state": str(pr.get("state") or ""),
                "base_ref": str(base.get("ref") or ""),
                "head_ref": str(head.get("ref") or ""),
                "head_sha": str(head.get("sha") or ""),
            }
        )
    return prs


def get_pull_request_info(repo: str, pr_number: int) -> dict[str, Any] | None:
    """Fetch PR metadata via the REST API (``gh api``).

    The reviewer gate already has the PR number and head SHA when CI completes.
    Fetching mergeability/base/labels via ``gh pr view`` would consume GraphQL
    quota and can silently strand green PRs when that quota is exhausted. This
    helper uses the REST/core quota and returns the same compact shape the gate
    previously received from ``gh pr view --json``.
    """
    out = _run_gh(["api", f"repos/{repo}/pulls/{pr_number}"])
    if out is None:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        log.warning("Failed to parse PR info JSON for %s#%d", repo, pr_number)
        return None
    if not isinstance(data, dict):
        log.warning("Unexpected PR info JSON for %s#%d", repo, pr_number)
        return None

    draft = data.get("draft")
    if not isinstance(draft, bool):
        log.warning("PR info missing draft state for %s#%d", repo, pr_number)
        return None

    base = data.get("base")
    head = data.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        log.warning("PR info missing base/head for %s#%d", repo, pr_number)
        return None

    base_ref = base.get("ref")
    base_sha = base.get("sha")
    head_sha = head.get("sha")
    if not isinstance(base_ref, str) or not base_ref:
        log.warning("PR info missing base ref for %s#%d", repo, pr_number)
        return None
    if not isinstance(base_sha, str) or not base_sha:
        log.warning("PR info missing base sha for %s#%d", repo, pr_number)
        return None
    if not isinstance(head_sha, str) or not head_sha:
        log.warning("PR info missing head sha for %s#%d", repo, pr_number)
        return None

    mergeable = data.get("mergeable")
    if mergeable is True:
        mergeable_state = "MERGEABLE"
    elif mergeable is False:
        mergeable_state = "CONFLICTING"
    else:
        mergeable_state = "UNKNOWN"

    labels = data.get("labels")
    if not isinstance(labels, list):
        labels = []

    title = _text_or_empty(data.get("title"))
    body = _text_or_empty(data.get("body"))

    number = data.get("number")
    if not isinstance(number, int):
        number = pr_number

    # A repository name is None when GitHub omits it (a deleted fork).
    return {
        "number": number,
        "mergeable": mergeable_state,
        "isDraft": draft,
        "baseRefName": base_ref,
        "baseRefOid": base_sha,
        "headRefOid": head_sha,
        "labels": labels,
        "title": title,
        "body": body,
        "headRepoFullName": repo_full_name(head.get("repo")),
        "baseRepoFullName": repo_full_name(base.get("repo")),
    }


def _text_or_empty(value: Any) -> str:
    """Return a PR text field, or an empty string when GitHub sent none."""
    return value if isinstance(value, str) else ""


def repo_full_name(repo: Any) -> str | None:
    """Return one PR side's ``owner/name``, or None when GitHub gives no repository."""
    name = repo.get("full_name") if isinstance(repo, dict) else None
    return name if isinstance(name, str) and name else None



def remove_review_label(repo: str, pr_number: int) -> bool:
    """Remove the consumed review label; an already-absent label is consumed."""
    from reviewer.reviewer_categories import LABEL_REVIEW

    try:
        result = run_gh(
            ["api", "-X", "DELETE", f"repos/{repo}/issues/{pr_number}/labels/{LABEL_REVIEW}"],
            timeout=_GH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("reviewer gate: removing review label from %s#%d did not run: %s", repo, pr_number, exc)
        return False
    if result.returncode != 0 and "404" not in (result.stderr or ""):
        log.warning(
            "reviewer gate: removing review label from %s#%d exited %d: %s",
            repo, pr_number, result.returncode, (result.stderr or "")[:200],
        )
        return False
    return True


def approve_action_required_workflow_runs(repo: str, head_sha: str) -> bool:
    """Approve every action-required Actions run for one exact fork head.

    GitHub requires a read of ``GET /actions/runs`` before the documented
    per-run ``POST /approve`` transition. A failed read or approval leaves the
    review label unapproved so a maintainer can retry by adding it again.
    """
    out = _run_gh(
        [
            "api", "--method", "GET", f"repos/{repo}/actions/runs",
            "-f", f"head_sha={head_sha}", "-f", "status=action_required",
            "--paginate", "--jq", ".workflow_runs[].id",
        ],
    )
    if out is None:
        return False
    run_ids = [line.strip() for line in out.splitlines() if line.strip()]
    if any(not run_id.isdecimal() for run_id in run_ids):
        log.warning("reviewer gate: invalid Actions run id while approving %s@%s", repo, head_sha[:8])
        return False
    for run_id in run_ids:
        if _run_gh(
            ["api", "--method", "POST", f"repos/{repo}/actions/runs/{run_id}/approve"],
        ) is None:
            return False
    return True
