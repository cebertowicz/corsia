"""A person's "Deliver immediately" choice, kept once on the server so every client and write path reads one value.

Stored in the ``settings`` table under one key per person (``users.id``), the same keyed-JSON shape other per-owner
settings use, so it needs no schema change. An unset value reads as on. Comments and re-opens still name their lane
explicitly; the server reads this value only where no client frame can carry the lane, a person's document save.
"""

from __future__ import annotations

from loguru import logger

from doceditor.feedback_batch_core import DOCUMENT_HELD, IMMEDIATE
from infra.settings import get_setting, set_setting

CHANGED = "doc_delivery_preference_changed"
_KEY = "doc_delivery_preference:"


def read(user_id: str, *, db=None) -> tuple[bool, bool]:
    """Return (immediate, stored): the person's choice, and whether they have made one."""
    value = (get_setting(_KEY + user_id, db=db) or {}).get("immediate")
    return (value, True) if isinstance(value, bool) else (True, False)


def write(user_id: str, immediate: bool, *, db=None) -> None:
    set_setting(_KEY + user_id, {"immediate": immediate}, db=db)


def lane_for_thread_owner(db, thread_id: str) -> str:
    """The lane a person's save queues in: the preference of the person who owns the document's thread."""
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    immediate, _ = read(str(row["user_id"]), db=db) if row and row["user_id"] else (True, False)
    return IMMEDIATE if immediate else DOCUMENT_HELD


def announce(user_id: str, immediate: bool) -> None:
    """Best-effort nudge so the person's other open clients follow; opening a document re-reads it."""
    from infra import state

    try:
        state.broadcast_to_user(user_id, {"type": CHANGED, "immediate": immediate})
    except Exception as exc:  # noqa: BLE001 - the stored value stays authoritative
        logger.warning("{} delivery failed: {}", CHANGED, type(exc).__name__)
