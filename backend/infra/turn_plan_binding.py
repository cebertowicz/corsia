"""Record, once, the plan attempt whose cost one active Turn carries.

The Turn/input transition layer owns every write to ``active_turns`` (contract
I3). Two Plans callers need this one transition: selection opened by a plan
mutation (``plans.admission_attempts.bind_turn``) and review verification
(``plans.review_usage.bind``). Turn start writes the same columns once, from
``plans.turn_admission.snapshot_after_admission``, inside
``infra.thread_inputs``.

The binding is write-once accounting, never execution authority. The first
attempt a Turn carries stays its snapshot; a later selection in the same Turn
does not rewrite it. Nothing reads these columns to select, conflict with or
refuse work, or to attribute a tool call (``plan_node_attempts`` alone
selects); artifact origin falls back to them only when the thread has no valid
selection.

The plan semantics stay with Plans: which attempt is selected, which commitment
owns its cost, and whether the Turn does the work or verifies it. This module
takes those already-resolved values and owns only the durable write, so
``infra`` still never imports ``plans``.
"""

from __future__ import annotations

import sqlite3

from infra.turn_usage_receipts import attribute_unbound_turn_usage
from infra.usage_row import PLAN_FIELDS

#: Every role a bound Turn can carry, matching the ``plan_usage_role`` CHECK in
#: ``infra.schema``. `execution` is the Turn doing the work; `delegation` a
#: child's Turn on a parent's commitment; `verification` a reviewer's Turn;
#: `correction` rework on an already-settled result. All four accrue to the same
#: original commitment, which is why none of them is a fresh attempt at it.
USAGE_ROLES = frozenset({"execution", "delegation", "verification", "correction"})


def bind_turn_to_plan_attempt(
    db: sqlite3.Connection,
    *,
    turn_id: str,
    plan_id: str,
    node_id: str,
    attempt_id: str,
    commitment_id: str,
    usage_role: str,
) -> bool:
    """Bind one still-unbound Turn to the plan attempt it executes or verifies.

    This function owns the rule that the bound attempt carries the whole Turn's
    cost (#579): in the same transaction as the binding, the Turn's earlier
    unbound ``llm_usage`` rows and pending receipts take the same five plan
    fields. Callers only bind; none pairs a second attribution step.

    Args:
        db: Connection already inside the caller's write transaction.
        turn_id: The exact active Turn to bind.
        plan_id: Plan owning the node.
        node_id: The selected node.
        attempt_id: The open attempt this Turn works under.
        commitment_id: Original commitment the Turn's cost accrues to.
        usage_role: One of ``USAGE_ROLES``.

    Returns:
        True when this call wrote the binding; False when the Turn is absent or
        already carries an attempt, which is left exactly as it was.

    Raises:
        ValueError: The usage role is not a known role.
    """
    if usage_role not in USAGE_ROLES:
        raise ValueError(f"unknown plan usage role: {usage_role!r}")
    changed = db.execute(
        """UPDATE active_turns
           SET plan_id=?,plan_node_id=?,plan_attempt_id=?,
               plan_commitment_id=?,plan_usage_role=?
           WHERE id=? AND plan_attempt_id IS NULL""",
        (plan_id, node_id, attempt_id, commitment_id, usage_role, turn_id),
    )
    if changed.rowcount != 1:
        return False
    turn = db.execute(
        "SELECT thread_id FROM active_turns WHERE id=?", (turn_id,)
    ).fetchone()
    binding = dict(
        zip(
            PLAN_FIELDS,
            (plan_id, node_id, attempt_id, commitment_id, usage_role),
            strict=True,
        )
    )
    attribute_unbound_turn_usage(
        db, thread_id=turn["thread_id"], turn_id=turn_id, binding=binding,
    )
    return True
