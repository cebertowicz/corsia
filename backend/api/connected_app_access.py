"""Who may act on a connected app: the refusal shape, the audit actor, and the person-only gate.

A leaf module, so the connected-app routes and the sign-in routes share one gate without importing each other.
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from infra.auth import is_person_session


def refusal(detail: str, status: int = 422) -> HTTPException:
    """Return a connected-app validation error with an explicit safe agent remedy."""
    headers = {"X-Corsia-Agent-Remedy": detail} if status == 403 else None
    return HTTPException(status, detail, headers=headers)


def audit_actor(user: dict) -> tuple[str, str]:
    """Return a durable, value-free actor identity for connected-app audit rows."""
    if is_person_session(user):
        actor = user.get("id")
        return "person", actor if isinstance(actor, str) and actor else "unknown"
    actor = user.get("thread_id") or user.get("id")
    return "agent", actor if isinstance(actor, str) and actor else "unknown"


def person_only(user: dict, action: str, app_id: str, *, copy: str, db: sqlite3.Connection) -> tuple[str, str]:
    """The caller's audit actor when it is the signed-in person; otherwise record ``refused_<action>`` and refuse with ``copy``."""
    actor = audit_actor(user)
    if not is_person_session(user):
        from connections.app_lifecycle_support import record_refusal

        record_refusal(app_id, f"refused_{action}", actor=actor, db=db)
        raise refusal(copy, 403)
    return actor
