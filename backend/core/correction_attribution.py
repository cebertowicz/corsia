"""Which plan node each correction fact belongs to: the single owner of that join.

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

Sent-back and self-caught facts come from plan events, not the classifier: each sits on the node its
``review_rejected`` or ``correction_created`` event names, and carries no classifier version.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

from core.correction_learning import TRIAGE_EVENT
from core.corrections_recording import (
    ACTIVE_VERSION_SETTING,
    COMMENT_SUBJECT_PREFIX,
    CHAT_COMMENT_SUBJECT_PREFIX,
    CORRECTION_EVENT,
    DECISION_SUBJECT_PREFIX,
    LABEL_CORRECTION,
    MESSAGE_SUBJECT_PREFIX,
    SENT_BACK_SUBJECT_PREFIX,
    SELF_CAUGHT_SUBJECT_PREFIX,
    LABEL_NOT_CORRECTION,
    SOURCE_KIND_DECISION_ANSWER,
    SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_CHAT_COMMENT,
    SOURCE_KIND_MESSAGE,
    chat_comment_source_parts_sql,
    decision_answer_arrived_at_sql,
    decision_answer_subject_key_sql,
    version_key_sql,
)

NOT_IN_WORKFLOW = "not_in_workflow"
UNATTRIBUTED = "unattributed"


def _ts(column: str) -> str:
    """One text form for stored timestamps written with either separator."""
    return f"replace({column},'T',' ')"


_chat_source = f"substr(f.subject_key,length('{CHAT_COMMENT_SUBJECT_PREFIX}') + 1)"
_chat_message, _chat_cid = chat_comment_source_parts_sql(_chat_source)

_SOURCES_SQL = f"""
    WITH facts AS (
      SELECT f.subject_key,f.agent_thread_id,f.attributes
      FROM outcome_events f
      WHERE f.event_name='{CORRECTION_EVENT}' AND f.retracted_at IS NULL
        AND json_extract(f.attributes,'$.label') IN ('{LABEL_CORRECTION}', '{LABEL_NOT_CORRECTION}')
    ), sources AS (
      SELECT '{SOURCE_KIND_MESSAGE}' AS source_kind,
        substr(f.subject_key,length('{MESSAGE_SUBJECT_PREFIX}') + 1) AS source_id,
        m.created_at AS source_at,m.thread_id,m.active_turn_id,f.attributes,m.thread_id AS target_thread,f.subject_key
      FROM facts f LEFT JOIN messages m
        ON m.id=substr(f.subject_key,length('{MESSAGE_SUBJECT_PREFIX}') + 1)
      WHERE f.subject_key LIKE '{MESSAGE_SUBJECT_PREFIX}%'
      UNION ALL
      SELECT '{SOURCE_KIND_DOC_COMMENT}',substr(f.subject_key,length('{COMMENT_SUBJECT_PREFIX}') + 1),
        c.created_at,f.agent_thread_id,NULL,f.attributes,f.agent_thread_id,f.subject_key
      FROM facts f LEFT JOIN doc_comments c
        ON c.id=substr(f.subject_key,length('{COMMENT_SUBJECT_PREFIX}') + 1)
      WHERE f.subject_key LIKE '{COMMENT_SUBJECT_PREFIX}%'
      UNION ALL
      SELECT '{SOURCE_KIND_CHAT_COMMENT}',substr(f.subject_key,length('{CHAT_COMMENT_SUBJECT_PREFIX}') + 1),
        c.created_at,m.thread_id,m.active_turn_id,f.attributes,m.thread_id,f.subject_key
      FROM facts f LEFT JOIN chat_comments c
        ON c.cid={_chat_cid} AND c.user_message_id={_chat_message}
      LEFT JOIN messages m ON m.id=c.user_message_id AND m.thread_id=c.thread_id
      WHERE f.subject_key LIKE '{CHAT_COMMENT_SUBJECT_PREFIX}%'
      UNION ALL
      SELECT '{SOURCE_KIND_DECISION_ANSWER}',substr(f.subject_key,length('{DECISION_SUBJECT_PREFIX}') + 1),
        {decision_answer_arrived_at_sql('d')},f.agent_thread_id,NULL,f.attributes,f.agent_thread_id,f.subject_key
      FROM facts f LEFT JOIN artifact_identities d ON f.subject_key={decision_answer_subject_key_sql('d')}
        AND d.integration='local' AND d.type='plan-decision'
      WHERE f.subject_key LIKE '{DECISION_SUBJECT_PREFIX}%'
    )
"""

_ATTRIBUTED_SQL = _SOURCES_SQL + f"""
    SELECT s.source_kind,s.source_id,s.source_at,s.attributes,
      (SELECT t.plan_node_id FROM active_turns t
        WHERE t.thread_id=s.thread_id AND t.id IS NOT s.active_turn_id
          AND {_ts('t.started_at')} < {_ts('s.source_at')}
        ORDER BY {_ts('t.started_at')} DESC,t.turn_order DESC LIMIT 1) AS node_id
    FROM sources s
"""


_ACTIVE_VERSION_ONLY = f"""{version_key_sql('s.attributes')}=(
      SELECT json_extract(value,'$.key') FROM settings WHERE key='{ACTIVE_VERSION_SETTING}')"""
"""Measurement reads the one activated classifier version; with none activated nothing matches."""

FIRST_ARRIVAL_SQL = _SOURCES_SQL + f"SELECT MIN(source_at) FROM sources s WHERE {_ACTIVE_VERSION_ONLY}"
"""The earliest arrival of a source the active version classified: where corrections began to be measured."""

_TIMED_SQL = _SOURCES_SQL + f"""
    SELECT s.source_kind,s.source_id,s.source_at,s.attributes,s.target_thread FROM sources s
    WHERE {_ts('s.source_at')} >= :start AND {_ts('s.source_at')} < :end AND {_ACTIVE_VERSION_ONLY}
"""

_COVERAGE_SQL = _SOURCES_SQL + f"""
    SELECT s.source_kind,s.source_id,s.source_at,s.attributes,s.target_thread,
      EXISTS(SELECT 1 FROM outcome_events t
        WHERE t.event_name='{TRIAGE_EVENT}' AND t.subject_key=s.subject_key) AS triaged,
      EXISTS(SELECT 1 FROM active_turns t
        WHERE t.thread_id=s.target_thread
          AND {_ts('t.started_at')} > {_ts('s.source_at')}) AS later_turn
    FROM sources s
    WHERE {_ts('s.source_at')} >= :start AND {_ts('s.source_at')} < :end AND {_ACTIVE_VERSION_ONLY}
"""


@dataclass(frozen=True)
class AttributedFact:
    """One correction source, the classifier version that counted it (None: a plan event, no classifier), and its node."""

    source_kind: str
    source_id: str
    source_at: str | None
    counter: str | None
    is_correction: bool
    node_id: str | None


def _latest_classifications(rows: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    """Keep the latest classification for each source, preserving first on ties."""
    latest: dict[tuple[str, str], tuple[str, sqlite3.Row]] = {}
    for row in rows:
        attributes = json.loads(row['attributes'])
        key = (str(row['source_kind']), str(row['source_id']))
        seen = str(attributes.get('classified_at') or '')
        if key not in latest or seen > latest[key][0]:
            latest[key] = (seen, row)
    return [row for _, row in latest.values()]


def _measured_attributed_facts(db: sqlite3.Connection) -> list[AttributedFact]:
    """Latest facts of Measurement's activated classifier, retaining task attribution."""
    rows = db.execute(_ATTRIBUTED_SQL + f" WHERE {_ACTIVE_VERSION_ONLY}").fetchall()
    facts = []
    for row in _latest_classifications(rows):
        attributes = json.loads(row['attributes'])
        facts.append(AttributedFact(
            str(row['source_kind']), str(row['source_id']), row['source_at'],
            str(attributes.get('asset_version')), attributes.get('label') == LABEL_CORRECTION,
            row['node_id']))
    return facts


def _plan_attributed_facts(db: sqlite3.Connection) -> list[AttributedFact]:
    """Plan-sourced correction facts, attributed to the work they corrected.

    Missing source events and missing corrected nodes stay unattributed.
    """
    rows = db.execute(f"""
        SELECT f.subject_key,e.created_at AS source_at,f.attributes,n.id AS source_node
        FROM outcome_events f
        LEFT JOIN plan_node_events e ON e.id = CASE
          WHEN f.subject_key LIKE '{SENT_BACK_SUBJECT_PREFIX}%'
            THEN substr(f.subject_key, length('{SENT_BACK_SUBJECT_PREFIX}') + 1)
          WHEN f.subject_key LIKE '{SELF_CAUGHT_SUBJECT_PREFIX}%'
            THEN substr(f.subject_key, length('{SELF_CAUGHT_SUBJECT_PREFIX}') + 1)
          END AND ((f.subject_key LIKE '{SENT_BACK_SUBJECT_PREFIX}%' AND e.event='review_rejected')
                OR (f.subject_key LIKE '{SELF_CAUGHT_SUBJECT_PREFIX}%' AND e.event='correction_created'))
        LEFT JOIN plan_nodes n ON n.id = CASE
          WHEN e.event='review_rejected' THEN e.node_id
          WHEN e.event='correction_created' AND json_valid(e.payload_json)
            THEN json_extract(e.payload_json,'$.source_node_id') END
        WHERE f.event_name=? AND f.retracted_at IS NULL
          AND (f.subject_key LIKE '{SENT_BACK_SUBJECT_PREFIX}%'
            OR f.subject_key LIKE '{SELF_CAUGHT_SUBJECT_PREFIX}%')
    """, (CORRECTION_EVENT,)).fetchall()
    facts = []
    for row in rows:
        attributes = json.loads(row['attributes'])
        kind = str(attributes.get('source_kind') or '')
        facts.append(AttributedFact(kind, str(attributes.get('source_id') or ''), row['source_at'],
                                    None, attributes.get('label') == LABEL_CORRECTION,
                                    row['source_node']))
    return facts


def attributed_facts(db: sqlite3.Connection, *, node_ids: Iterable[str] | None = None) -> list[AttributedFact]:
    """Latest active person classifications and plan-sourced correction facts, optionally only those
    attributed to selected plan nodes."""
    selected = None if node_ids is None else frozenset(node_ids)
    if selected is not None and not selected:
        return []
    facts = _measured_attributed_facts(db) + _plan_attributed_facts(db)
    return facts if selected is None else [fact for fact in facts if fact.node_id in selected]


def person_corrections(db: sqlite3.Connection, start: str, end: str) -> list[tuple[str, str | None]]:
    """The person's corrections arriving in [start, end) as (arrival time, target thread), one per source.

    ``start`` and ``end`` are stored UTC instants (``YYYY-MM-DD HH``), so rows outside the window are
    dropped by the read and never decoded.

    Messages, chat comments, document comments and decision answers only: a sent-back review is the reviewer's
    and is counted as a rejection. Only facts of the activated classifier version are read
    (``active_corrections_version``): a version still backfilling, or never activated, is not
    served. A source that version classified more than once counts once, by its latest fact.
    A source whose arrival cannot be resolved has no time and is left out.
    """
    rows = db.execute(_TIMED_SQL, {"start": start, "end": end}).fetchall()
    timed = (row for row in rows if row["source_at"] is not None)
    return [
        (str(row["source_at"]), None if row["target_thread"] is None else str(row["target_thread"]))
        for row in _latest_classifications(timed)
        if json.loads(row["attributes"]).get("label") == LABEL_CORRECTION
    ]


@dataclass(frozen=True)
class CorrectionCoverage:
    """One correction's arrival, target thread, whether it was triaged, and whether the thread has run since."""

    arrived: str
    thread: str | None
    triaged: bool
    later_turn: bool


def person_correction_coverage(db: sqlite3.Connection, start: str, end: str) -> list[CorrectionCoverage]:
    """The corrections ``person_corrections`` counts, each with its triage and idle state.

    ``triaged`` is a ``learning_loop.correction_triage`` fact on the same subject, any tier.
    ``later_turn`` is a turn started in the target thread after the correction arrived; an archived
    thread that never ran again, or a deleted one (its turn rows go with it), has none and reads as idle.
    """
    rows = db.execute(_COVERAGE_SQL, {"start": start, "end": end}).fetchall()
    timed = (row for row in rows if row["source_at"] is not None)
    return [
        CorrectionCoverage(str(row["source_at"]), None if row["target_thread"] is None else str(row["target_thread"]),
                           bool(row["triaged"]), bool(row["later_turn"]))
        for row in _latest_classifications(timed)
        if json.loads(row["attributes"]).get("label") == LABEL_CORRECTION
    ]


def workflow_run_of(node_id: str | None, parents: Mapping[str, str | None], runs: Iterable[str]) -> str:
    """The workflow run a node belongs to: the nearest run at or above it, else Not in a workflow.

    ``parents`` maps each plan node to its parent; ``runs`` are the run nodes. A fact with no node is
    Unattributed.
    """
    if node_id is None:
        return UNATTRIBUTED
    roots, seen = runs if isinstance(runs, (set, frozenset)) else set(runs), set()
    current: str | None = node_id
    while current is not None and current not in seen:
        if current in roots:
            return current
        seen.add(current)
        current = parents.get(current)
    return NOT_IN_WORKFLOW
