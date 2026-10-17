"""GitHub operations that hold the reviewer on fork heads until a maintainer approves."""

from __future__ import annotations

import logging
import re
import subprocess

from reviewer.gh_api import run_gh
from reviewer.reviewer_categories import LABEL_REVIEW

log = logging.getLogger(__name__)

_GH_TIMEOUT_S = 30.0
_APPROVER_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?")


def _gh(args: list[str], what: str) -> subprocess.CompletedProcess[str] | None:
    """Run one ``gh`` command; None (logged) when it could not run at all."""
    try:
        return run_gh(args, timeout=_GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("fork hold: %s did not run: %s", what, exc)
        return None


def sender_can_approve(repo: str, login: str | None) -> bool:
    """Whether a login has write access to the repository; anything unverifiable is no."""
    if not login or not _LOGIN.fullmatch(login):
        return False
    result = _gh(
        ["api", f"repos/{repo}/collaborators/{login}/permission", "--jq", ".permission"],
        f"read {login}'s permission on {repo}",
    )
    if result is None or result.returncode != 0:
        return False
    return result.stdout.strip() in _APPROVER_PERMISSIONS


def remove_review_label(repo: str, pr_number: int) -> bool:
    """Consume the review label so adding it again fires a fresh ``labeled`` event.

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
