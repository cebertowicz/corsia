"""Only a signed-in person writes or clears a provider key, with an audit line for each.

An agent or service token can still use a provider; it cannot replace or erase
the key behind it. Every attempt is logged by kind, name, action and token kind
(``session``, ``api``, ``lifecycle``): never a value, a password or a token.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from infra.auth import is_person_session

log = logging.getLogger("corsia.key_audit")

PERSON_ONLY = "Enter the key in Providers while signed in."


def _token_kind(user: dict) -> str:
    return str(user.get("token_kind") or "unknown")


def require_person_for_key(user: dict, *, kind: str, name: str, action: str) -> None:
    """Refuse a key write or clear unless the caller holds a person's session."""
    if not is_person_session(user):
        log.warning(
            "key %s refused: kind=%s name=%s token_kind=%s", action, kind, name, _token_kind(user),
        )
        raise HTTPException(403, PERSON_ONLY)


def audit_key_write(user: dict, *, kind: str, name: str, action: str) -> None:
    """Record that a key was written or cleared."""
    log.info("key %s: kind=%s name=%s token_kind=%s", action, kind, name, _token_kind(user))
