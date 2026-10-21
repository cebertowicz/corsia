"""Which plan node each classified owner source belongs to: the single owner of that join.

The workflow verdict (``core.workflow_verdict_read``) calls this module, and any later Measurement
read of corrections by task must too; none re-derives the rule. For one ``measurement.correction``
fact, the corrected work is the plan node of the latest turn in the source's target thread that
started before the source arrived. A message's own turn is excluded. A turn bound to no task means
the agent was between tasks when it replied (a chat, a message or an event), and the fact is
**Unattributed**. Nothing is inferred in its place: the reply turn's task or the agent's last
released task put a third or more of such corrections on the wrong task. Turns are bound from
Sep 9, 2026, so earlier corrections are Unattributed by construction.

Times are the source's persisted arrival time, never the fact's ``ts``: most facts were backfilled
long after the source. Every source joins by its canonical ``outcome_events.subject_key``; the
recorded target thread is carried in ``agent_thread_id``. Decision answers intentionally count in
the workflow verdict because they are correction sources that feed the learning loop. A node outside
every workflow is **Not in a workflow**, which is not the same as Unattributed; ``workflow_run_of``
makes that split. Facts labelled ``unreadable`` are never counted (``core.corrections_recording``),
and neither are retracted ones.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

from core.corrections_recording import (
    COMMENT_SUBJECT_PREFIX,
    CORRECTION_EVENT,
    DECISION_SUBJECT_PREFIX,
    LABEL_CORRECTION,
    MESSAGE_SUBJECT_PREFIX,
    LABEL_NOT_CORRECTION,
    SOURCE_KIND_DECISION_ANSWER,
    SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_MESSAGE,
    decision_answer_arrived_at_sql,
    decision_answer_subject_key_sql,
)

NOT_IN_WORKFLOW = "not_in_workflow"
UNATTRIBUTED = "unattributed"


def _ts(column: str) -> str:
    """One text form for stored timestamps written with either separator."""
    return f"replace({column},'T',' ')"


_ATTRIBUTED_SQL = f"""
    WITH facts AS (
      SELECT f.subject_key,f.agent_thread_id,f.attributes
      FROM outcome_events f
      WHERE f.event_name=:event AND f.retracted_at IS NULL
        AND json_extract(f.attributes,'$.label') IN (:correction, :not_correction)
    ), sources AS (
      SELECT '{SOURCE_KIND_MESSAGE}' AS source_kind,
        substr(f.subject_key,length('{MESSAGE_SUBJECT_PREFIX}') + 1) AS source_id,
        m.created_at AS source_at,m.thread_id,m.active_turn_id,f.attributes
      FROM facts f LEFT JOIN messages m
        ON m.id=substr(f.subject_key,length('{MESSAGE_SUBJECT_PREFIX}') + 1)
      WHERE f.subject_key LIKE '{MESSAGE_SUBJECT_PREFIX}%'
      UNION ALL
      SELECT '{SOURCE_KIND_DOC_COMMENT}',substr(f.subject_key,length('{COMMENT_SUBJECT_PREFIX}') + 1),
        c.created_at,f.agent_thread_id,NULL,f.attributes
      FROM facts f LEFT JOIN doc_comments c
        ON c.id=substr(f.subject_key,length('{COMMENT_SUBJECT_PREFIX}') + 1)
      WHERE f.subject_key LIKE '{COMMENT_SUBJECT_PREFIX}%'
      UNION ALL
      SELECT '{SOURCE_KIND_DECISION_ANSWER}',substr(f.subject_key,length('{DECISION_SUBJECT_PREFIX}') + 1),
        {decision_answer_arrived_at_sql('d')},f.agent_thread_id,NULL,f.attributes
      FROM facts f LEFT JOIN artifact_identities d ON f.subject_key={decision_answer_subject_key_sql('d')}
        AND d.integration='local' AND d.type='plan-decision'
      WHERE f.subject_key LIKE '{DECISION_SUBJECT_PREFIX}%'
    )
    SELECT s.source_kind,s.source_id,s.source_at,s.attributes,
      (SELECT t.plan_node_id FROM active_turns t
        WHERE t.thread_id=s.thread_id AND t.id IS NOT s.active_turn_id
          AND {_ts('t.started_at')} < {_ts('s.source_at')}
        ORDER BY {_ts('t.started_at')} DESC,t.turn_order DESC LIMIT 1) AS node_id
    FROM sources s
"""


@dataclass(frozen=True)
class AttributedFact:
    """One classified owner source, the classifier version that counted it, and its node."""

    source_kind: str
    source_id: str
    source_at: str | None
    counter: str
    is_correction: bool
    node_id: str | None


def attributed_facts(db: sqlite3.Connection, *, node_ids: Iterable[str] | None = None) -> list[AttributedFact]:
    """Readable correction facts, optionally only where attribution lands in selected nodes."""
    query = _ATTRIBUTED_SQL
    params: dict[str, object] = {
        "event": CORRECTION_EVENT, "correction": LABEL_CORRECTION, "not_correction": LABEL_NOT_CORRECTION,
    }
    if node_ids is not None:
        encoded = json.dumps(sorted(set(node_ids)))
        if encoded == "[]":
            return []
        query = f"SELECT * FROM ({query}) WHERE node_id IN (SELECT value FROM json_each(:node_ids))"
        params["node_ids"] = encoded
    rows = db.execute(query, params).fetchall()
    facts = []
    for row in rows:
        attributes = json.loads(row["attributes"])
        facts.append(AttributedFact(
            str(row["source_kind"]),
            str(row["source_id"]),
            None if row["source_at"] is None else str(row["source_at"]),
            str(attributes.get("asset_version")),
            attributes.get("label") == LABEL_CORRECTION,
            None if row["node_id"] is None else str(row["node_id"]),
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
