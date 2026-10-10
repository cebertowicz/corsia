"""The emails the person is already signed in with, read before CORSIA has a database.

Install asks for the person's email before their account exists, so this reads the
ambient Claude Code and Codex Defaults without the database or a server. Both reads
are bounded and read-only: they never sign in or out and never return a token.
"""
from __future__ import annotations

import json
import logging
import subprocess

from connections import binary_from_config
from connections.account_identity import codex_identity
from connections.account_profiles import profile_for

log = logging.getLogger(__name__)

_STATUS_SECONDS = 10.0


def _claude_email() -> str:
    binary = binary_from_config("claude-code", {})
    if binary is None:
        return ""
    from subagents import cc_env

    profile = profile_for("claude-code")
    env = cc_env.clean_claude_env()
    env.pop(profile.home_variable, None)
    try:
        done = subprocess.run(
            [binary, *profile.commands["status"]], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=_STATUS_SECONDS, check=False)
        report = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if not isinstance(report, dict) or report.get("loggedIn") is not True:
        return ""
    return profile.facts_from_status(report).get("email", "")


def _codex_email() -> str:
    from subagents.codex_env import default_codex_home

    return codex_identity(default_codex_home()).get("email", "")


def signed_in_emails() -> list[str]:
    """Distinct emails of the signed-in Claude Code then Codex Defaults; a failed read adds none."""
    found: list[str] = []
    for read in (_claude_email, _codex_email):
        try:
            email = read().strip()
        except Exception:  # noqa: BLE001 - a failed read only means no suggestion
            log.warning("Could not read a signed-in email", exc_info=True)
            continue
        if email and email.lower() not in {known.lower() for known in found}:
            found.append(email)
    return found
