"""Small ``gh api`` JSON helpers used by reviewer automation."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)


def run_gh(
    args: list[str], *, input_text: str | None = None, timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run one ``gh`` command as the agents' GitHub account when there is one.

    Callers own all result and failure policy.
    """
    from connections import github_agent

    account = github_agent.view()
    if account.cause == "verifying":  # first read after a restart: wait for the answer once
        account = github_agent.verify_now()
    binary, env = github_agent.backend_gh(account)
    return subprocess.run(
        [binary, *args], input=input_text, capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )


def gh_api_patch_json(api_path: str, payload: dict[str, Any]) -> bool:
    try:
        result = run_gh(
            ["api", "-X", "PATCH", api_path, "--input", "-"],
            input_text=json.dumps(payload), timeout=30,
        )
        if result.returncode != 0:
            log.error(
                "gh api PATCH %s failed (rc=%d): %s",
                api_path, result.returncode, (result.stderr or "")[:300],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("gh api PATCH %s timed out", api_path)
        return False
    except FileNotFoundError:
        log.error("gh CLI not found")
        return False


def gh_api_post_json(api_path: str, payload: dict[str, Any]) -> bool:
    try:
        result = run_gh(
            ["api", "-X", "POST", api_path, "--input", "-"],
            input_text=json.dumps(payload), timeout=30,
        )
        if result.returncode != 0:
            log.error(
                "gh api POST %s failed (rc=%d): %s",
                api_path, result.returncode, result.stderr[:300],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("gh api POST %s timed out", api_path)
        return False
    except FileNotFoundError:
        log.error("gh CLI not found")
        return False
