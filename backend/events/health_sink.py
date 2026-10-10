"""Delivery-health sink resolution (coordination MVP S4b).

A delivery-health failure must reach the *narrowest live* triage owner, and the
chain must never dead-end on an archived/non-live thread. The terminal is a
non-archivable workspace system sentinel: it has no DB row to archive, so it is
always live. Dead-letter parking is the visible queue on that sentinel — a
failure is never a silent drop (RFC §7).
"""

from __future__ import annotations

#: Non-archivable workspace health sentinel. Not a DB thread (cannot be archived
#: or paused) — the guaranteed-live terminus of every triage/health chain.
WORKSPACE_HEALTH_SINK = "workspace-health-sink"


def is_health_sink(thread_id: str | None) -> bool:
    """True for the workspace health sentinel."""
    return thread_id == WORKSPACE_HEALTH_SINK


def resolve_live_health_owner(
    candidate_chain: list[str | None], *, db=None
) -> str:
    """Narrowest live triage owner in the chain, else the workspace sentinel.

    Re-resolves past archived / non-live / missing candidates (AC-2.7) and ALWAYS
    returns a live terminus — never ``None``, never a dead-end.
    """
    from events import delivery

    for thread in candidate_chain:
        if thread and not is_health_sink(thread) and delivery.thread_exists_and_active(thread, db=db):
            return thread
    return WORKSPACE_HEALTH_SINK
