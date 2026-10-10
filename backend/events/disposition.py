"""Delivery disposition table (coordination MVP S4b).

The single source of the wake-vs-digest decision, keyed by
``(relationship, event_class, lifecycle)``. It is Event Delivery's ONLY decision
(ED5): a disposition is ``wake`` or ``digest`` — never a user-attention class
(warning / decision / report / user-raise are artifact workflows, not delivery
outcomes, ED18). ``parent_chain`` is structurally hard-wired to ``digest`` so a
parent-chain recipient can never be woken and chains never propagate wakes
(ED19).
"""

from __future__ import annotations

WAKE = "wake"
DIGEST = "digest"

_DEAD_LIFECYCLES = frozenset({"archived", "tombstone"})

def disposition(
    relationship: str, event_class: str, lifecycle: str = "active"
) -> str:
    """Return ``wake`` or ``digest`` for one recipient relationship.

    - ``parent_chain`` → always ``digest`` (structural, no wake chaining).
    - a dead (archived/tombstone) artifact never wakes.
    - ``owner`` wakes on an actionable event, digests on informational.
    - ``triage`` / ``repair`` owners wake (they must act — ED8).
    - ``subscriber`` digests by default (subscriber wake is explicit and rare,
      gated by a declared affected-subscriber policy not modeled here).
    """
    if relationship == "parent_chain":
        return DIGEST  # hard-wired before any other consideration (ED19).
    if lifecycle in _DEAD_LIFECYCLES:
        return DIGEST
    if relationship == "owner":
        return WAKE if event_class == "actionable" else DIGEST
    if relationship in ("triage", "repair"):
        return WAKE
    if relationship == "subscriber":
        return DIGEST
    return DIGEST  # unknown relationship → never wake unexpectedly.


def delivery_disposition(
    relationship: str, event_class: str, lifecycle: str = "active"
) -> str:
    """The combined Event-Delivery taxonomy label (one of the five ED5 values).

    Folds the ``wake|digest`` decision together with the recipient relationship:
    owner/triage/repair → ``owner_*`` (triage and repair owners are woken as
    owners, ED8); subscriber/parent_chain → ``subscriber_*``; everything with no
    recipient → ``dead_letter``. Never a user-attention class.
    """
    if relationship == "none":
        return "dead_letter"
    wd = disposition(relationship, event_class, lifecycle)
    if relationship in ("owner", "triage", "repair"):
        return "owner_wake" if wd == WAKE else "owner_digest"
    if relationship in ("subscriber", "parent_chain"):
        return "subscriber_wake" if wd == WAKE else "subscriber_digest"
    return "dead_letter"
