"""Resolve the pull requests a commit belongs to, for check_suite routing."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from connections.github_run import run_gh

log = logging.getLogger(__name__)

_GH_TIMEOUT_S = 30.0


def _run_gh(args: list[str], timeout: float = _GH_TIMEOUT_S) -> str | None:
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
        log.warning("gh %s exited %d: %s", args[:3], result.returncode, result.stderr[:200])
        return None
    return result.stdout


def get_prs_for_sha(repo: str, head_sha: str) -> list[dict[str, Any]] | None:
    """Return PR metadata for the PRs a commit SHA belongs to, via the REST API.

    GitHub delivers ``check_suite.pull_requests`` empty intermittently, which
    strands a green PR because its owner cannot be resolved.
    ``GET /repos/{repo}/commits/{sha}/pulls`` resolves a head in this repository.
    Uses the REST/core quota, separate from GraphQL.

    Returns compact PR metadata (``[]`` when the commit has no associated PR),
    or ``None`` on a fetch/parse error (never a silent empty-as-success).
    """
    out = _run_gh(["api", f"repos/{repo}/commits/{head_sha}/pulls"], timeout=30.0)
    if out is None:
        return None
    try:
        data = json.loads(out) if out.strip() else []
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
        raw_base, raw_head = pr.get("base"), pr.get("head")
        base: dict[str, Any] = raw_base if isinstance(raw_base, dict) else {}
        head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
        number_value = pr.get("number")
        try:
            number = int(str(number_value))
        except (TypeError, ValueError):
            log.warning("Failed to parse PR number for %s@%s: %r", repo, head_sha[:8], number_value)
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
