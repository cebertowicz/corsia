"""Publish GHA commit status checks for the LLM reviewer.

Uses ``gh api`` to set commit statuses on the PR's head SHA. The verdict
context is ``qa/llm-reviewer``. A second context, ``qa/llm-reviewer-present``,
posts ``success`` only where the gate marks that SHA reviewed after a genuine
verdict is published and its owner wake did not fail, so a required check can
demand that a review ran without making a block unwaivable. An override skip
posts ``failure`` there with the reason, since no review ran. The context
carries no outcome: the verdict context alone says pass or block.

States:
- ``pending``: Review in progress (or retrying after rate limit).
- ``success``: Zero block findings. Flags and nits publish as comments and
  are dispositioned by the author under sdlc-process; they do not gate.
- ``failure``: ≥1 block finding, or model-unavailable.
- ``error``: Reviewer threw an exception / timed out / hit max_turns.

Only a block gates, and this module owns that contract for the whole reviewer.
A ``flag`` is defined to the model as "likely wrong or risky but the reviewer
is not certain", and sdlc-process already requires every flag to be fixed,
dispositioned, or routed as follow-up. Gating on flags made that disposition
unreachable -- a correctly declined flag still posted red, leaving another
code change as the only act that could turn the status green. Measured before
the change: PR #1190 ran 15+ rounds with zero block findings and never went
green; PR #1236 escaped only by labelling ``qa/override-reviewer``, which
discards every finding at once.

Non-verdict guard: a standing ``success`` on the context (e.g. a human
waiver) is senior to every non-verdict post. Infra ``error``/``failure``
posts never overwrite it — the gate's error-retry loop cannot stomp a newer
success another writer posted — and ``pending`` does not overwrite it either:
a re-fired review proceeds silently and only its terminal verdict replaces
the waiver. An identical standing red is not re-posted (notification spam).
Genuine review verdicts (``set_failure(verdict=True)`` and ``set_success``)
are senior to any standing status and always post.
"""

from __future__ import annotations

import json
import logging
import subprocess
from uuid import uuid4
from typing import Any

from reviewer.gh_api import run_gh
from reviewer.gh_helpers import (
    STATUS_STAMP_INSTANCE_CHARS,
    STATUS_WRITE_ID_CHARS,
    STATUS_WRITE_STAMP,
    get_commit_status_for_context,
)
from reviewer.reviewer_categories import (
    STATUS_CHECK_CONTEXT,
    STATUS_PRESENCE_CONTEXT,
)

log = logging.getLogger(__name__)

_STATUS_DESCRIPTION_MAX = 140
# Created once when this module is loaded by a reviewer-serving process. Unlike
# workspace identity, this distinguishes concurrent CORSIA runtimes while
# remaining non-secret and stable for that process lifetime.
RUNTIME_INSTANCE_ID = uuid4().hex


def _max_status_text_length(instance_id: str) -> int:
    """Return the status-text budget after reserving visible provenance."""
    stamp_length = (
        len(instance_id[:STATUS_STAMP_INSTANCE_CHARS])
        + STATUS_WRITE_ID_CHARS
        + len("[m: w:]")
    )
    return _STATUS_DESCRIPTION_MAX - stamp_length - 1


def _stamp_status_description(description: str) -> tuple[str, str, str]:
    """Return GitHub text plus full instance and status-write identities."""
    instance_id = RUNTIME_INSTANCE_ID
    write_id = uuid4().hex
    stamp = (
        f"[m:{instance_id[:STATUS_STAMP_INSTANCE_CHARS]} "
        f"w:{write_id[:STATUS_WRITE_ID_CHARS]}]"
    )
    stamped = f"{description[:_max_status_text_length(instance_id)]} {stamp}"
    return stamped, instance_id, write_id


def _without_status_write_stamp(description: str) -> str:
    """Return the status text used by the duplicate-notification guard."""
    return STATUS_WRITE_STAMP.sub("", description)


def set_status(
    repo: str,
    sha: str,
    state: str,
    description: str,
    target_url: str | None = None,
    context: str = STATUS_CHECK_CONTEXT,
) -> bool:
    """Set a commit status check via the GitHub API.

    Args:
        repo: Full repository name (``owner/repo``).
        sha: The commit SHA to set status on.
        state: One of ``pending``, ``success``, ``failure``, ``error``.
        description: Short description (max 140 chars per GitHub API).
        target_url: Optional URL for the "Details" link on the status.
        context: Commit-status context; defaults to the reviewer verdict context.

    Returns:
        True if the status was set successfully.
    """
    if state not in ("pending", "success", "failure", "error"):
        log.error("Invalid status state: %s", state)
        return False

    # GitHub caps descriptions at 140 characters. Keep the verdict first and
    # reserve space for visible provenance on every pipeline-owned write.
    description, instance_id, write_id = _stamp_status_description(description)

    body: dict[str, Any] = {
        "state": state,
        "description": description,
        "context": context,
    }
    if target_url:
        body["target_url"] = target_url

    try:
        result = run_gh(
            [
                "api",
                f"repos/{repo}/statuses/{sha}",
                "--method", "POST",
                "--input", "-",
            ],
            input_text=json.dumps(body),
            timeout=30,
        )
        if result.returncode != 0:
            log.error(
                "reviewer-status: failed status write repo=%s sha=%s state=%s "
                "instance=%s write_id=%s rc=%d: %s",
                repo, sha, state, instance_id, write_id, result.returncode,
                result.stderr[:300],
            )
            return False
        log.info(
            "reviewer-status: posted status write repo=%s sha=%s state=%s "
            "instance=%s write_id=%s",
            repo, sha, state, instance_id, write_id,
        )
        return True
    except subprocess.TimeoutExpired:
        log.error(
            "reviewer-status: failed status write repo=%s sha=%s state=%s "
            "instance=%s write_id=%s error=timeout",
            repo, sha, state, instance_id, write_id,
        )
        return False
    except FileNotFoundError:
        log.error(
            "reviewer-status: failed status write repo=%s sha=%s state=%s "
            "instance=%s write_id=%s error=gh-not-found",
            repo, sha, state, instance_id, write_id,
        )
        return False


def _skip_non_verdict_post(repo: str, sha: str, state: str, description: str) -> bool:
    """Return True when a non-verdict status post must be suppressed.

    Reads the newest standing status on the reviewer context and suppresses:

    - any non-verdict post over a standing ``success`` — success (e.g. a human
      waiver) is senior to everything except a real review verdict. For an
      infra red that means the error-retry loop never stomps a newer success
      another writer posted; for ``pending`` it means a re-fired review
      proceeds silently and only its terminal verdict may replace the waiver
      (unguarded pending would erase the waiver before the reviewer even ran);
    - an identical red (same state + description) — already visible, so a
      re-post only spams notifications while retrying.

    Fails open: when the standing status is unreadable (rate limit, gh error)
    the post proceeds — losing the guard is safer than losing the signal.
    """
    standing = get_commit_status_for_context(repo, sha, STATUS_CHECK_CONTEXT)
    if not standing:  # None (unreadable) or {} (no status yet) — post proceeds
        return False
    if standing.get("state") == "success":
        if state == "pending":
            log.warning(
                "reviewer-status: standing success on %s for %s@%s respected; "
                "review proceeding silently — pending not posted",
                STATUS_CHECK_CONTEXT, repo, sha[:8],
            )
        else:
            log.warning(
                "reviewer-status: standing success on %s for %s@%s respected — "
                "suppressing infra %s post (%r)",
                STATUS_CHECK_CONTEXT, repo, sha[:8], state, description,
            )
        return True
    if (
        state in ("error", "failure")
        and standing.get("state") == state
        and _without_status_write_stamp(str(standing.get("description") or ""))
        == description[:_max_status_text_length(RUNTIME_INSTANCE_ID)]
    ):
        log.info(
            "reviewer-status: identical %s already standing on %s for %s@%s — "
            "not re-posting",
            state, STATUS_CHECK_CONTEXT, repo, sha[:8],
        )
        return True
    return False


def set_pending(repo: str, sha: str, description: str = "Review in progress") -> bool:
    """Set status to pending (review started or retrying).

    Guarded: the in-progress signal never stomps a standing success (e.g. a
    human waiver). The review itself still runs — silently — and its terminal
    verdict post (``set_failure(verdict=True)`` or ``set_success``), which is
    senior to any waiver, posts normally at the end.
    """
    if _skip_non_verdict_post(repo, sha, "pending", description):
        return True
    return set_status(repo, sha, "pending", description)


def set_success(repo: str, sha: str, description: str) -> bool:
    """Set status to success (no block-severity findings).

    Unguarded: success is itself the senior non-verdict state. Truncates like
    its sibling setters so every wrapper hands ``set_status`` the exact
    140-char form GitHub stores.
    """
    return set_status(repo, sha, "success", description)


def set_presence(repo: str, sha: str) -> bool:
    """Mark that a genuine reviewer verdict is published at this SHA.

    Called only after the owner wake did not fail: delivered, or no owner
    thread is registered for the PR.

    A failed marker posts ``error`` on the presence context, best effort, so
    the miss is visible without replacing the verdict already published.
    """
    if set_status(
        repo, sha, "success", "reviewer verdict published at this head",
        context=STATUS_PRESENCE_CONTEXT,
    ):
        return True
    set_status(
        repo, sha, "error", "verdict published; presence marker post failed",
        context=STATUS_PRESENCE_CONTEXT,
    )
    return False


def set_presence_absent(repo: str, sha: str, reason: str) -> bool:
    """Show on the presence context that no review ran at this SHA, and why."""
    return set_status(repo, sha, "failure", reason, context=STATUS_PRESENCE_CONTEXT)


def set_failure(repo: str, sha: str, description: str, *, verdict: bool = False) -> bool:
    """Set status to failure (review verdict or infra failure).

    ``verdict=True`` marks a genuine review verdict (block/flag findings): it
    is senior to any standing status — including a success waiver placed
    before it — and always posts. The default (infra failure such as
    model-unavailable) is guarded by ``_skip_non_verdict_post``; a policy skip
    returns True because the standing status is the intended visible outcome.
    """
    if not verdict and _skip_non_verdict_post(repo, sha, "failure", description):
        return True
    return set_status(repo, sha, "failure", description)


def set_error(repo: str, sha: str, description: str) -> bool:
    """Set status to error (reviewer crashed or timed out).

    Error states are always infra outcomes, never review verdicts, so every
    error post is guarded by ``_skip_non_verdict_post``; a policy skip returns
    True because the standing status is the intended visible outcome.
    """
    if _skip_non_verdict_post(repo, sha, "error", description):
        return True
    return set_status(repo, sha, "error", description)
