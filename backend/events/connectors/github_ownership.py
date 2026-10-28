"""GitHub-specific validation for PR body ownership declarations."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Any

from loguru import logger

_OWN_LOGIN: str | None = None
_REFRESH_LOCK = threading.Lock()
_REFRESH_DONE = threading.Event()
_REFRESH_DONE.set()
_LAST_REFRESH_MONOTONIC = float("-inf")
_REFRESH_TIMEOUT_SECONDS = 2
_REFRESH_BACKOFF_SECONDS = 300
_UUID4_THREAD_ID = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_PROJECT_THREAD_ID = rf"(?:{_UUID4_THREAD_ID}|[0-9a-f]{{8}})"
_OWNER_THREAD_LINE = re.compile(
    rf"^[ \t]*owner[ \t]+thread[ \t]+id[ \t]*:[ \t]*`?({_PROJECT_THREAD_ID})`?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def refresh_own_login(*, timeout: float = _REFRESH_TIMEOUT_SECONDS) -> str | None:
    """Perform one bounded, single-flight refresh of the authenticated login."""
    global _LAST_REFRESH_MONOTONIC, _OWN_LOGIN
    if not _REFRESH_LOCK.acquire(blocking=False):
        _REFRESH_DONE.wait(timeout)
        return _OWN_LOGIN
    _REFRESH_DONE.clear()
    _LAST_REFRESH_MONOTONIC = time.monotonic()
    try:
        try:
            result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("github ownership: own-login refresh failed: {}", type(exc).__name__)
            _OWN_LOGIN = None
            return None
        login = result.stdout.strip() if result.returncode == 0 else ""
        if not login:
            logger.warning("github ownership: own-login refresh was unavailable")
            _OWN_LOGIN = None
            return None
        _OWN_LOGIN = login
        return login
    finally:
        _REFRESH_DONE.set()
        _REFRESH_LOCK.release()


def cached_own_login() -> str | None:
    """Return the cached GitHub login without performing I/O."""
    return _OWN_LOGIN


def _ensure_own_login() -> str | None:
    """Return the cached login, retrying an unknown one off the calling thread.

    Routing calls this on the event loop, so it never runs ``gh`` inline: an
    unknown login starts at most one background refresh per retry interval
    and this event proceeds without a validated declaration.
    """
    global _LAST_REFRESH_MONOTONIC
    if _OWN_LOGIN:
        return _OWN_LOGIN
    if _REFRESH_LOCK.locked():
        return None
    if time.monotonic() - _LAST_REFRESH_MONOTONIC < _REFRESH_BACKOFF_SECONDS:
        return None
    _LAST_REFRESH_MONOTONIC = time.monotonic()
    threading.Thread(
        target=refresh_own_login, name="github-own-login-refresh", daemon=True,
    ).start()
    return None


def declared_owner_thread_ids(body: str) -> set[str]:
    """Return every lowercased ``Owner thread ID:`` value a PR body declares."""
    return {match.lower() for match in _OWNER_THREAD_LINE.findall(body)}


def declaration_facts(payload: dict[str, Any]) -> dict[str, str]:
    """Return a connector-validated owner declaration from a PR-shaped payload."""
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not issue.get("pull_request"):
            return {}
        pull_request = issue

    author = pull_request.get("user")
    body = pull_request.get("body")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(body, str):
        return {}
    declared_ids = declared_owner_thread_ids(body)
    if len(declared_ids) != 1:
        return {}
    if not isinstance(login, str) or not is_own(login):
        return {}
    return {"declared_owner_thread": declared_ids.pop()}


def is_own(login: str) -> bool:
    """Whether ``login`` is a verified login of CORSIA's own: the host's, else the agents' account.

    The agents' account is read only for another author: one indexed read and one
    ``stat``, no subprocess. An unknown or unverified identity matches nothing.
    """
    own_login = cached_own_login() or _ensure_own_login()
    if own_login and login.casefold() == own_login.casefold():
        return True
    from connections import github_agent

    try:
        agent_login = github_agent.agent_login(github_agent.view())
    except Exception as exc:  # noqa: BLE001 - an unreadable account proves no ownership
        logger.warning("github ownership: agents' account read failed: {}", type(exc).__name__)
        return False
    return agent_login is not None and login.casefold() == agent_login.casefold()
