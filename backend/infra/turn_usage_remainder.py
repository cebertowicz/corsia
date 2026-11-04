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

import json
from collections.abc import Callable, Collection, Sequence
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


#: The receipt's owner: the commitment its binding names, or the Turn's thread.
_RECEIPT_COMMITMENT = "json_extract(receipts.value,'$.binding.plan_commitment_id')"
_RECEIPT_THREAD = "turns.thread_id"
#: A Turn in one of these has finished, so a call it made can no longer be on its way.
SETTLED_TURNS = ("completed", "error")


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
    return _owed_counts(
        db, commitment_ids, _RECEIPT_COMMITMENT,
        lambda entry, turn: (entry.binding if entry.binding is not None else turn)["plan_commitment_id"],
    )


def pending_thread_measurement_counts(
    db: Any, thread_ids: Sequence[str],
) -> dict[str, dict[str, int]]:
    """The measurements each thread is owed but has not projected, keyed by thread id, then Turn id.

    The same ledger knowledge as ``pending_measurement_counts``, owed to the thread whose Turn
    holds the receipt or the held call, whatever plan task each call bills.
    """
    return _owed_counts(db, thread_ids, _RECEIPT_THREAD, lambda _entry, turn: turn["thread_id"])


def _owed_counts(
    db: Any, owners: Sequence[str], receipt_owner: str, held_owner: Callable[[HeldUsage, Any], str | None],
) -> dict[str, dict[str, int]]:
    """Owed measurements for ``owners``, counted by ``receipt_owner`` (SQL) and ``held_owner`` (held calls)."""
    if not owners:
        return {}
    wanted = set(owners)
    owed: dict[str, dict[str, int]] = {}
    # Left to choose, SQLite walked every Turn in id order for the GROUP BY
    # rather than the few the partial index holds.
    for row in db.execute(
        f"""SELECT {receipt_owner} AS owner, turns.id AS turn_id, COUNT(*) AS pending
            FROM active_turns AS turns INDEXED BY idx_active_turns_pending_usage,
                 json_each(turns.usage_receipts_json) AS receipts
            WHERE turns.usage_receipts_json <> '{{}}'
              AND {receipt_owner} IN (SELECT value FROM json_each(?))
            GROUP BY owner, turns.id""",
        (json.dumps(sorted(wanted)),),
    ).fetchall():
        owed.setdefault(str(row["owner"]), {})[str(row["turn_id"])] = int(row["pending"])
    for entry in held_turn_usage():
        turn = _held_owed_turn(db, entry)
        owner = held_owner(entry, turn) if turn is not None else None
        if owner in wanted:
            turns = owed.setdefault(owner, {})
            turns[str(entry.turn_id)] = turns.get(str(entry.turn_id), 0) + 1
    return owed


def owes_settled_measurement(turn_id: str, status: str, measured: Collection[str], pending: Collection[str]) -> bool:
    """Whether a Turn ended holding neither a ledger row nor a pending receipt: no measurement at all."""
    return status in SETTLED_TURNS and turn_id not in pending and turn_id not in measured


def thread_measurement_remainder(db: Any, thread_ids: Sequence[str]):
    """Read owed measurements and settled turns before pricing the named threads' rows."""
    ids = sorted(set(thread_ids))
    if not ids:
        return {}, []
    owed = pending_thread_measurement_counts(db, ids)
    settled = db.execute(
        "SELECT id,thread_id,status FROM active_turns "
        "WHERE thread_id IN (SELECT value FROM json_each(?)) "
        f"AND status IN ({','.join('?' for _ in SETTLED_TURNS)})",
        (json.dumps(ids), *SETTLED_TURNS),
    ).fetchall()
    return owed, settled


def short_threads(owed, settled, priced) -> set[str]:
    """Identify threads with owed or unmeasured turns using the pricing read's turn IDs."""
    short = set(owed)
    for row in settled:
        thread = str(row["thread_id"])
        if owes_settled_measurement(row["id"], row["status"], priced[thread]["turns"], owed.get(thread, {})):
            short.add(thread)
    return short


def _held_owed_turn(db: Any, entry: HeldUsage) -> Any | None:
    """The Turn row a held call's row will be written under, or None when nothing is owed.

    A held call already written, or already counted as its Turn's pending
    receipt, is not owed twice; one whose Turn row is gone is not owed.
    """
    if db.execute("SELECT 1 FROM llm_usage WHERE id=?", (entry.identity,)).fetchone():
        return None
    turn = db.execute(
        "SELECT usage_receipts_json,plan_commitment_id,thread_id FROM active_turns WHERE id=?",
        (entry.turn_id,),
    ).fetchone()
    if turn is None or entry.identity in _decode_receipts(str(turn["usage_receipts_json"])):
        return None
    return turn
