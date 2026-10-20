"""The person's own GitHub login, as last observed in their own ``gh`` home.

GitHub's connection refresh (``connections.refresh_github_connection``) stores it on
the ``github`` connection row, and only a definite login replaces it, so a cold start
reads the last known login at once. The agents' account is never ready while this is
unknown: nothing else proves the bot is not the person.
"""
from __future__ import annotations

import logging
import threading
import time

from connections.models import get_connection

log = logging.getLogger(__name__)

_RETRY_SECONDS = 60.0
_probe_lock = threading.Lock()
_last_probe = float("-inf")


def person_login(*, db=None) -> str | None:
    """The stored login of the person's own GitHub account, or None if never observed."""
    row = get_connection("github", db=db)
    login = ((row or {}).get("config") or {}).get("login")
    return login if isinstance(login, str) and login else None


def same_login(left: str | None, right: str | None) -> bool:
    """GitHub logins compare without case."""
    return left is not None and right is not None and left.casefold() == right.casefold()


def signin_refusal(_home: str, facts: dict[str, str]) -> str | None:
    """Refuse only a CORSIA sign-in that is the person's own GitHub login.

    CORSIA's GitHub CLI owns its GH_CONFIG_DIR and intentionally stores its
    credential there so verified agent shells do not depend on login Keychain access.
    """
    if same_login(person_login(), facts.get("login")):
        return "person_account"
    return None


def request_probe() -> None:
    """Start at most one background GitHub refresh per retry interval to learn the person's login."""
    global _last_probe
    if time.monotonic() - _last_probe < _RETRY_SECONDS or not _probe_lock.acquire(blocking=False):
        return
    _last_probe = time.monotonic()
    threading.Thread(target=_probe, name="github-person-login", daemon=True).start()


def _probe() -> None:
    from connections import refresh_github_connection

    try:
        refresh_github_connection(keep_row_on_failed_probe=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable login stays unknown and is retried later
        log.warning("github person login probe failed: %s", type(exc).__name__)
    finally:
        _probe_lock.release()


def reset_for_tests() -> None:
    global _last_probe
    _last_probe = float("-inf")
