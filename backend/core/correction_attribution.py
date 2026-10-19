"""Which plan node each classified owner message belongs to: the single owner of that join.

The workflow verdict (``core.workflow_verdict_read``) calls this module, and any later Measurement
read of corrections by task must too; none re-derives the rule. For one ``measurement.correction``
fact, the corrected work is the plan node of the turn the message answered: the latest turn in the
message's thread that started before the message, excluding the message's own turn. A turn bound to
no task means the agent was between tasks when it replied (a chat, a message or an event), and the
fact is **Unattributed**. Nothing is inferred in its place: the reply turn's task or the agent's last
released task put a third or more of such corrections on the wrong task. Turns are bound from
Sep 9, 2026, so earlier corrections are Unattributed by construction.

Times are the MESSAGE's ``created_at``, never the fact's ``ts``: most facts were backfilled long
after the message. A node outside every workflow is **Not in a workflow**, which is not the same
as Unattributed; ``workflow_run_of`` makes that split. Facts labelled ``unreadable`` are never
counted (``core.corrections_recording``), and neither are retracted ones.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

from core.corrections_recording import CORRECTION_EVENT, LABEL_CORRECTION, LABEL_NOT_CORRECTION

NOT_IN_WORKFLOW = "not_in_workflow"
UNATTRIBUTED = "unattributed"


def _ts(column: str) -> str:
    """One text form for stored timestamps written with either separator."""
    return f"replace({column},'T',' ')"


_ATTRIBUTED_SQL = f"""
    SELECT m.id AS message_id, m.created_at AS message_at, f.attributes AS attributes,
      (SELECT t.plan_node_id FROM active_turns t
        WHERE t.thread_id=m.thread_id AND t.id IS NOT m.active_turn_id
          AND {_ts('t.started_at')} < {_ts('m.created_at')}
        ORDER BY {_ts('t.started_at')} DESC, t.turn_order DESC LIMIT 1) AS node_id
    FROM outcome_events f
    JOIN messages m ON m.id=json_extract(f.attributes,'$.message_id')
    WHERE f.event_name=:event AND f.retracted_at IS NULL
      AND json_extract(f.attributes,'$.label') IN (:correction, :not_correction)
"""


@dataclass(frozen=True)
class AttributedFact:
    """One classified owner message, the classifier version that counted it, and its node."""

    message_id: str
    message_at: str
    counter: str
    is_correction: bool
    node_id: str | None


def attributed_facts(db: sqlite3.Connection) -> list[AttributedFact]:
    """Every readable correction fact with the plan node it is attributed to, or None (Unattributed)."""
    rows = db.execute(_ATTRIBUTED_SQL, {
        "event": CORRECTION_EVENT, "correction": LABEL_CORRECTION, "not_correction": LABEL_NOT_CORRECTION,
    }).fetchall()
    facts = []
    for row in rows:
        attributes = json.loads(row["attributes"])
        facts.append(AttributedFact(
            str(row["message_id"]), str(row["message_at"]), str(attributes.get("asset_version")),
            attributes.get("label") == LABEL_CORRECTION, None if row["node_id"] is None else str(row["node_id"]),
        ))
    return facts


def workflow_run_of(node_id: str | None, parents: Mapping[str, str | None], runs: Iterable[str]) -> str:
    """The workflow run a node belongs to: the nearest run at or above it, else Not in a workflow.

    ``parents`` maps each plan node to its parent; ``runs`` are the run nodes. A fact with no node is
    Unattributed.
    """
    if node_id is None:
        return UNATTRIBUTED
    roots, seen = set(runs), set()
    current: str | None = node_id
    while current is not None and current not in seen:
        if current in roots:
            return current
        seen.add(current)
        current = parents.get(current)
    return NOT_IN_WORKFLOW
