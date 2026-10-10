"""Stateless continuation cursors bound to one plan read's snapshot.

A cursor names where the next page of one read starts, never what the page
contains. A status section's cursor also carries the clock its first page was
cut at, and is bound to the section's ordered row identities; a whole-plan
(``FULL``) cursor is bound to the plan revision. Every continuation recomputes
from canonical state and refuses unless that binding still holds, so pages
never mix two plan states, duplicate a row, or skip one. Nothing is persisted:
a cursor can be stale, never wrong.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from plans.errors import PlanError

STALE = (
    "status cursor is stale: the plan changed, or the cursor belongs to another "
    "section. Read plan_read() again for a fresh snapshot"
)
FULL = "full"
FULL_STALE = (
    "full-plan cursor is stale: the plan revision moved, or the cursor belongs to a "
    "status section. Read plan_read(mode='full') again for a fresh first page"
)


def token(plan_id: str, members: list[dict]) -> str:
    """Hash the section's ordered row identities -- membership, order, revision, state."""
    source = [plan_id, [f"{n['id']}:{n['revision']}:{n['state']}" for n in members]]
    return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


def encode(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(value: str | None, *, plan_id: str, section: str | None) -> dict | None:
    """Return a cursor's payload for this plan and read, or refuse it as stale.

    ``section`` is a status section or ``FULL``; only a status cursor carries
    its first page's clock.
    """
    if value is None:
        return None
    if section is None:
        raise PlanError("a status cursor continues one section: pass section='current' or 'recent'")
    stale = FULL_STALE if section == FULL else STALE
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, TypeError, AttributeError):
        raise PlanError(stale) from None
    if (
        not isinstance(payload, dict) or payload.get("v") != 1
        or payload.get("p") != plan_id or payload.get("s") != section
        or not isinstance(payload.get("o"), int) or payload["o"] < 0
        or (section != FULL and not isinstance(payload.get("a"), str))
        or not isinstance(payload.get("t"), str)
    ):
        raise PlanError(stale)
    return payload
