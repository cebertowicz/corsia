"""Frozen vendor identity at Turn and detached checkpoint dispatch boundaries."""
from __future__ import annotations

from connections.vendor_accounts import VENDORS
from connections import fact_memo
from infra.database import get_db


@fact_memo.memoized_reader
def model_vendor(model: str, *, db) -> str | None:
    """Identify an exact catalog model's harness; unknown/native stays unknown."""
    from llm.resolution import _target_row

    model = model.strip()
    if not model:
        # A thread with no model has no vendor; never read the catalog for it.
        return None
    row = _target_row(model, db=db)
    harness = row.get("harness") if row else None
    return str(harness) if harness in VENDORS else None


def admitted_model_state(thread_id: str, state, *, db, movable: bool = False) -> tuple[int, str, bool] | None:
    """Hold model-less or ineligible input before freezing a paid Turn.

    With ``movable``, a hold that is only the project not permitting the thread's account
    (rollover on) is passed through and flagged, for the claim to try a move and re-check.
    """
    from connections.account_holds import refresh_model_hold

    if state is None or not str(state["model"] or "").strip():
        return None
    model = str(state["model"])
    hold = refresh_model_hold(thread_id, model, wake=False, db=db)
    passed = False
    if hold:
        from connections.account_rollover import movable_hold

        if not (movable and movable_hold(thread_id, model, hold, db=db)):
            return None
        passed = True
    return int(state["execution_epoch"]), model, passed


def selected_account_id(thread_id: str, provider: str, *, db) -> str | None:
    """Use the shared project-aware selection owner in the Turn transaction."""
    from connections.account_policy import select_account

    return select_account(thread_id, provider, wake=False, db=db)


def turn_account_id(thread_id: str, model: str, *, db) -> str | None:
    provider = model_vendor(model, db=db)
    return selected_account_id(thread_id, provider, db=db) if provider else None


def checkpoint_account_id(thread_id: str, writer_harness: str, *, db=None) -> str | None:
    """Read the ACTUAL writer's account independently of the source Turn.

    Detached checkpoint admission does not rebind the source thread. An existing
    writer-vendor binding is stable; an unbound writer uses the actual default
    launch route. Runtime passes this same frozen identity to the launch owner.
    """
    if writer_harness not in VENDORS:
        return None
    connection = db or get_db()
    try:
        from connections.account_policy import chosen_account_id

        return chosen_account_id(thread_id, writer_harness, db=connection)
    finally:
        if db is None:
            connection.close()
