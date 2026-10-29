"""The person's own ``gh`` sign-in probe, including gh releases without ``auth status --json``."""

from __future__ import annotations

import logging
import subprocess

from connections.github_agent_home import LOGIN, parse_status
from connections.github_read_request import (
    AUTH_PROBE, SIGNED_OUT, SINGLE_ACCOUNT_PROBE, UNSUPPORTED_FLAG,
)

log = logging.getLogger(__name__)

# gh added ``auth status --json`` in v2.81.0 (cli/cli#11544, merged 2025-09-25, first
# released 2025-10-01); ``--active`` and ``--hostname`` are older. A gh without the
# JSON flag exits non-zero with "unknown flag: --json".
MIN_GH_VERSION = "2.81.0"
UPDATE_GH_REASON = f"Update gh to {MIN_GH_VERSION} or later"


class GhUpdateRequired(Exception):
    """The installed gh cannot report its sign-in in a form this probe reads."""


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("gh auth check failed: %s", e)
        return None


def gh_status(binary: str) -> tuple[bool | None, str | None]:
    """Sign-in and login from one ``gh auth status --json hosts``; (None, None) if the probe failed.

    Read by the same parser as the agents' account, so the login comes from the
    same subprocess. A gh too old for ``--json`` falls back to ``_status_without_json``.
    """
    result = _run([binary, *AUTH_PROBE, "--json", "hosts"])
    if result is None:
        return None, None
    if result.returncode != 0 and UNSUPPORTED_FLAG.search(result.stderr.decode("utf-8", "replace")):
        return _status_without_json(binary)
    authed, facts = parse_status(result.returncode, result.stdout)
    return authed, facts.get("login")


def _status_without_json(binary: str) -> tuple[bool | None, str | None]:
    """Sign-in from plain ``gh auth status``, then the login from ``gh api user``.

    ``gh api user`` is the same call the agents' ownership check makes, so both
    resolve the same identity. A definite "not logged in" is signed out; a gh
    that cannot yield a login needs the update, never "not signed in".
    """
    status = _run([binary, *SINGLE_ACCOUNT_PROBE])
    if status is None:
        return None, None
    text = (status.stdout + status.stderr).decode("utf-8", "replace")
    if status.returncode != 0 and SIGNED_OUT.search(text):
        return False, None
    who = _run([binary, "api", "user", "--jq", ".login"])
    if who is None:
        return None, None
    login = who.stdout.decode("utf-8", "replace").strip() if who.returncode == 0 else ""
    if LOGIN.match(login):
        return True, login
    raise GhUpdateRequired(UPDATE_GH_REASON)
