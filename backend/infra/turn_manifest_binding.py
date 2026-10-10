"""Record which guidance list one active Turn is delivering.

The Turn/input transition layer owns every write to ``active_turns`` (contract
I3). ``core.guidance_manifest`` decides the list and its recording status;
this module owns only the durable write of that result.

The binding is attribution, never execution authority: nothing reads these
columns to select, admit or settle work. A Native mid-Turn checkpoint
continuation rebinds the Turn to its new list, so usage receipts stored after
it name the list their calls were made under.
"""

from __future__ import annotations

from typing import Any


def bind_turn_manifest(
    db: Any, *, thread_id: str, turn_id: str, manifest_id: str | None, status: str,
) -> None:
    """Set the Turn's list id and recording status in the caller's transaction."""
    db.execute(
        """UPDATE active_turns
           SET context_manifest_id=?, context_manifest_status=?
           WHERE id=? AND thread_id=?""",
        (manifest_id, status, turn_id, thread_id),
    )


def mark_guidance_load_status(
    db: Any, *, thread_id: str, turn_id: str, status: str,
) -> None:
    """Set the Turn's mid-Turn skill-read recording status in the caller's transaction."""
    db.execute(
        "UPDATE active_turns SET guidance_load_status=? WHERE id=? AND thread_id=?",
        (status, turn_id, thread_id),
    )
