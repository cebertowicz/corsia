"""Owner pin/unpin transitions on artifact registrations.

Pinning is the user's persistence override on one registration. It never
mutates ``retired_at`` — that column is the single permanent record of
when a row was retired (its node settled, its artifact ended, or only an
ownership change put it there), owned by
``registry.node_retirement`` and cleared only by a genuine re-registration of
a still-live artifact (``registry.identity_bridge``). Pinning only changes what the pane shows
*right now* (``relationship_state``, ``surfacing``); unpinning re-reads the
untouched retirement fact via ``registry.retirement_predicate`` instead of
storing a second copy of it.
"""

from __future__ import annotations

from infra.database import write_transaction
from infra.product_events import emit_product_event
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES as _OWNER_REGISTRATION_STATES,
    PANE_REGISTRATION_STATES as _PANE_REGISTRATION_STATES,
    normalized_row_by_id as _normalized_row_by_id,
)
from registry.registration_events import _append_registration_event, _now
from registry.retirement_predicate import retirement_to_restore


def _apply_pin_update(db, reg, target_state: str, pinned: bool, restore, now: str) -> None:
    """Write one pin transition.

    Pinning an actively retired row only unhides it (``surfacing``); the
    retirement fact on ``retired_at`` is left alone. Unpinning writes
    back the surfacing ``retirement_to_restore`` computed from that same
    untouched column — a pure flag flip when nothing is currently retired.
    """
    if restore:
        db.execute(
            """UPDATE artifact_registrations
               SET relationship_state=?, surfacing=?, updated_at=?
               WHERE id=?""",
            (target_state, restore.surfacing, now, reg["id"]),
        )
        return
    db.execute(
        """UPDATE artifact_registrations
           SET relationship_state=?,
               surfacing=CASE WHEN ? AND surfacing='settled'
                              THEN 'visible' ELSE surfacing END,
               updated_at=?
           WHERE id=?""",
        (target_state, pinned, now, reg["id"]),
    )


def update_pinned(
    artifact_id: str,
    *,
    pinned: bool,
    parent_thread_id: str,
    actor_thread_id: str | None = None,
    causal_event_id: str | None = None,
    db=None,
) -> dict | None:
    """Set one owner's persistence override without changing identity state.

    Pinning a registration that node settlement previously retired restores it to
    the pane. A deliberately quiet registration remains quiet.
    """
    target_state = "owner-pinned" if pinned else "owner-active"
    with write_transaction(db, scope="update_pinned") as _db:
        states = _OWNER_REGISTRATION_STATES
        placeholders = ",".join("?" for _ in states)
        reg = _db.execute(
            f"""SELECT id, surfacing, relationship_state, retired_at
                FROM artifact_registrations
                WHERE artifact_id=? AND parent_thread_id=?
                  AND relationship_state IN ({placeholders})
                LIMIT 1""",
            (artifact_id, parent_thread_id, *states),
        ).fetchone()
        if reg is None:
            return None
        if reg["relationship_state"] != target_state:
            now = _now()
            restore = (
                None if pinned
                else retirement_to_restore(_db, reg["id"])
            )
            _apply_pin_update(_db, reg, target_state, pinned, restore, now)
            _append_registration_event(
                _db,
                artifact_id=artifact_id,
                event_type="pin_changed",
                actor_thread_id=actor_thread_id,
                authority="pane-persistence",
                reason=None,
                source_path="registry.pinning.update_pinned",
                causal_event_id=causal_event_id,
                affected_thread_id=parent_thread_id,
                old_owner_thread_id=None,
                new_owner_thread_id=None,
                old_relationship_state=reg["relationship_state"],
                new_relationship_state=target_state,
                metadata=(
                    {
                        "pinned": True,
                        "old_surfacing": reg["surfacing"],
                        "old_retired_at": reg["retired_at"],
                    }
                    if pinned
                    else {"pinned": False}
                ),
                now=now,
            )
            emit_product_event(
                "artifact_pane",
                "pin_changed",
                thread_id=parent_thread_id,
                properties={"pinned": pinned, "contract_version": 1},
            )
        return _normalized_row_by_id(
            _db,
            artifact_id,
            parent_thread_id=parent_thread_id,
            registration_states=_PANE_REGISTRATION_STATES,
        )
