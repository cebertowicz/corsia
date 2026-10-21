"""Only a signed-in person changes where a project's models may run, with an audit line for each.

The project cloud switch and a server's private mark decide which models may receive a
thread's content. An agent or service token can read them; it cannot widen or narrow them.
Every attempt is logged by subject, name and token kind (``session``, ``api``, ``lifecycle``).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from infra.auth import is_person_session

log = logging.getLogger("corsia.locality_audit")

PERSON_ONLY = "Only the signed-in person can change where models run. Change it in CORSIA while signed in."


def _token_kind(user: dict) -> str:
    return str(user.get("token_kind") or "unknown")


def require_person_for_locality(user: dict, *, subject: str, name: str) -> None:
    """Refuse a locality-policy write unless the caller holds a person's session."""
    if not is_person_session(user):
        log.warning(
            "locality policy write refused: subject=%s name=%s token_kind=%s",
            subject, name, _token_kind(user),
        )
        raise HTTPException(403, PERSON_ONLY)
    log.info("locality policy write: subject=%s name=%s token_kind=%s", subject, name, _token_kind(user))
