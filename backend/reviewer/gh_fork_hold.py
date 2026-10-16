"""GitHub operations that hold the reviewer on fork heads until a maintainer approves."""

from __future__ import annotations

import logging
import subprocess

from reviewer.gh_api import run_gh
from reviewer.reviewer_categories import LABEL_REVIEW

log = logging.getLogger(__name__)

_GH_TIMEOUT_S = 30.0


def _gh(args: list[str], what: str) -> subprocess.CompletedProcess[str] | None:
    """Run one ``gh`` command; None (logged) when it could not run at all."""
    try:
        return run_gh(args, timeout=_GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("fork hold: %s did not run: %s", what, exc)
        return None


def remove_review_label(repo: str, pr_number: int) -> bool:
    """Remove the review label so a later approval fires a fresh ``labeled`` event.

    A label that is already absent counts as removed.
    """
    result = _gh(
        ["api", "-X", "DELETE", f"repos/{repo}/issues/{pr_number}/labels/{LABEL_REVIEW}"],
        f"remove review label from {repo}#{pr_number}",
    )
    if result is None:
        return False
    if result.returncode != 0 and "404" not in (result.stderr or ""):
        log.warning(
            "fork hold: removing review label from %s#%d exited %d: %s",
            repo, pr_number, result.returncode, (result.stderr or "")[:200],
        )
        return False
    return True


def waiting_run_ids(repo: str, head_sha: str) -> list[int] | None:
    """Ids of the workflow runs GitHub holds for approval at one head; None on failure."""
    result = _gh(
        [
            "api",
            f"repos/{repo}/actions/runs?head_sha={head_sha}&status=action_required&per_page=100",
            "--jq", ".workflow_runs[].id",
        ],
        f"list waiting runs for {repo}@{head_sha[:8]}",
    )
    if result is None or result.returncode != 0:
        if result is not None:
            log.warning("fork hold: listing waiting runs exited %d: %s", result.returncode, (result.stderr or "")[:200])
        return None
    return [int(line) for line in result.stdout.split() if line.isdigit()]


def approve_waiting_runs(repo: str, head_sha: str) -> int | None:
    """Approve every workflow run held for approval at this head; count, or None if unlistable."""
    run_ids = waiting_run_ids(repo, head_sha)
    if run_ids is None:
        return None
    approved = 0
    for run_id in run_ids:
        result = _gh(
            ["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/approve"],
            f"approve run {run_id}",
        )
        if result is not None and result.returncode == 0:
            approved += 1
        elif result is not None:
            log.warning(
                "fork hold: approving run %d exited %d: %s",
                run_id, result.returncode, (result.stderr or "")[:200],
            )
    return approved
