"""Helpers that write trial receipts through the production insertion seam."""
from __future__ import annotations

from pathlib import Path

from core.lesson_delivery import TrialDelivery, _insert


def write_trial_receipt(
    db, *, lesson_id: str = "lesson", behavioral_sha256: str = "a" * 64,
    thread_id: str = "owner", node_id: str = "task", variant: str | None = "with",
    checkpoint_generation_id: str | None = None, created_at: str | None = None,
) -> str:
    """Write one receipt using the same ``_insert`` -> ledger path as task delivery."""
    trial = TrialDelivery(
        lesson_id, behavioral_sha256, Path(f"/skills/{lesson_id}/SKILL.md"),
        f"trial:{lesson_id}".encode(), f"skills/{lesson_id}/SKILL.md", "Trial guidance.",
    )
    _insert(
        db, thread_id=thread_id, node_id=node_id, trial=trial, variant=variant,
        checkpoint_generation_id=checkpoint_generation_id,
    )
    row = db.execute("SELECT id FROM guidance_loads ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row is not None
    receipt_id = str(row[0])
    if created_at is not None:
        db.execute("UPDATE guidance_loads SET created_at=? WHERE id=?", (created_at, receipt_id))
    return receipt_id
