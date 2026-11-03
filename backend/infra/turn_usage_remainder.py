"""Whether a Turn's calls were receipted, read from the ledger itself.

An external harness records each model call as its own receipt as it
arrives. Its terminal reports the Turn's whole volume as a fallback: it is
written only when the ledger holds no receipt of the Turn's calls, so a Turn
measured per call is never counted twice and a Turn that reported no call
still lands whole. A receipt the entry refused is on record as that call's
defect; it is not guessed back in by subtraction.

The same knowledge says how short a Turn's total currently is: a receipt
still pending is a measurement committed but not yet written, so a cost
projection counts those rows as unknown rather than inferring from row
presence that the Turn was measured.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from infra.retained_usage import HeldUsage, held_turn_usage
from infra.turn_usage_receipts import _decode_receipts, _receipt_id


def holds_receipts(db: Any, turn_id: str, *, source: str, except_key: str) -> bool:
    """Whether the ledger holds any receipt for ``turn_id`` from ``source``.

    Rows already projected, receipts still pending projection, and calls
    held through a locked database until the Turn ends all count: a
    projection consumes its receipt in the same transaction. The receipt
    under ``except_key`` -- the terminal's own -- is left out.
    """
    excluded = _receipt_id(turn_id, except_key)
    if any(
        entry.identity != excluded and entry.source == source
        for entry in held_turn_usage(turn_id)
    ):
        return True
    row = db.execute(
        "SELECT 1 FROM llm_usage WHERE turn_id=? AND source=? AND id<>? LIMIT 1",
        (turn_id, source, excluded),
    ).fetchone()
    if row is not None:
        return True
    turn = db.execute(
        "SELECT usage_receipts_json FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone()
    pending = _decode_receipts(str(turn["usage_receipts_json"])) if turn is not None else {}
    return any(
        identity != excluded and payload.get("source") == source
        for identity, payload in pending.items()
    )


def pending_measurement_counts(
    db: Any, commitment_ids: Sequence[str],
) -> dict[str, dict[str, int]]:
    """How many measurements each commitment is owed but has not projected.

    A receipt is committed on its Turn before the ledger row it becomes, so
    one still pending is one measurement no total can include -- the exact
    amount by which that commitment's spend is understated. It is owed to the
    commitment in the receipt's own binding, which is the one its row will be
    written with, not the Turn's start snapshot: a Turn that moves from one
    task to the next bills each call to the task current when it began.
    Keyed by commitment id, then Turn id; absent keys owe nothing. This is the
    ledger's own knowledge: a consumer cannot read it from ``llm_usage``,
    where a partially projected Turn is indistinguishable from a fully
    measured one.

    Only Turns still holding receipts are read, through the partial index
    reconciliation uses, and the parameters are the caller's own bounded
    request rather than the Turn history those commitments have accumulated.
    A call held until its Turn ends (``infra.retained_usage``) is owed the
    same way, until the terminal writes it.
    """
    if not commitment_ids:
        return {}
    marks = ",".join("?" for _ in commitment_ids)
    owed: dict[str, dict[str, int]] = {}
    # Left to choose, SQLite walked every Turn in id order for the GROUP BY
    # rather than the few the partial index holds.
    for row in db.execute(
        f"""SELECT json_extract(receipts.value,'$.binding.plan_commitment_id')
                       AS commitment_id,
                   turns.id AS turn_id, COUNT(*) AS pending
            FROM active_turns AS turns INDEXED BY idx_active_turns_pending_usage,
                 json_each(turns.usage_receipts_json) AS receipts
            WHERE turns.usage_receipts_json <> '{{}}'
              AND json_extract(receipts.value,'$.binding.plan_commitment_id')
                  IN ({marks})
            GROUP BY commitment_id, turns.id""",
        tuple(commitment_ids),
    ).fetchall():
        owed.setdefault(str(row["commitment_id"]), {})[str(row["turn_id"])] = int(
            row["pending"]
        )
    for entry in held_turn_usage():
        commitment_id = _held_commitment(db, entry)
        if commitment_id is not None and commitment_id in commitment_ids:
            turns = owed.setdefault(commitment_id, {})
            turns[str(entry.turn_id)] = turns.get(str(entry.turn_id), 0) + 1
    return owed


def _held_commitment(db: Any, entry: HeldUsage) -> str | None:
    """The commitment a held call's row will bill, or None when nothing is owed.

    A held call already written, or already counted as its Turn's pending
    receipt, is not owed twice; one whose Turn row is gone lands unbound.
    """
    if db.execute("SELECT 1 FROM llm_usage WHERE id=?", (entry.identity,)).fetchone():
        return None
    turn = db.execute(
        "SELECT usage_receipts_json,plan_commitment_id FROM active_turns WHERE id=?",
        (entry.turn_id,),
    ).fetchone()
    if turn is None or entry.identity in _decode_receipts(str(turn["usage_receipts_json"])):
        return None
    binding = entry.binding if entry.binding is not None else turn
    return binding["plan_commitment_id"]
