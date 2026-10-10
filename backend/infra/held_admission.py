"""Build a held admission from the canonical connection-availability read."""

from __future__ import annotations

import sqlite3

from connections.harness_usage import connection_availability
from infra.database import get_db
from infra.thread_input_types import HeldAdmission


def limited_admission(
    thread_id: str, model: str, *, db: sqlite3.Connection | None = None,
) -> HeldAdmission | None:
    """Return the hold a canonically limited connection gives this model.

    The one predicate for "limited": admission, the checkpoint fold gate and
    delegation status all read it, so they cannot disagree. It only reads.
    """
    connection = db or get_db()
    try:
        from connections.account_policy import chosen_account_id
        from infra.vendor_turn_binding import model_vendor

        provider = model_vendor(model, db=connection)
        identity = chosen_account_id(thread_id, provider, db=connection) if provider else None
        availability = connection_availability(model, account_id=identity, db=connection)
    finally:
        if db is None:
            connection.close()
    if availability.state not in {"limited_with_reset", "limited_without_reset"}:
        return None
    return HeldAdmission(
        str(availability.connection or "unknown"),
        availability.reason,
        availability.resets_at,
        thread_id,
        model,
    )


def connection_limit_gate(
    thread_id: str,
    model: str,
    *,
    probe=None,
    hold_expired_at_startup: bool = False,
    db: sqlite3.Connection,
) -> tuple[bool, HeldAdmission | None]:
    """Return whether account-limit admission settled this candidate.

    Order: a probe's own checks, then the active-limit hold, then the startup
    hold. The probe's ``started`` marker is claimed later, in the same write,
    only once the Turn is certain to start (``infra.thread_inputs``).
    """
    if probe is not None:
        from infra.connection_limit_release import probe_claim_allowed

        if not probe_claim_allowed(probe, model, thread_id=thread_id, db=db):
            return True, None
    if held := limited_admission(thread_id, model, db=db):
        return True, held
    if hold_expired_at_startup:
        from infra.connection_limit_release import startup_expired_admission

        if held := startup_expired_admission(thread_id, model, db=db):
            return True, held
    return False, None
