"""The calls the person has already settled for a thread, newest first.

Two kinds, each read from its one canonical record:

- ``decision``: a plan decision the person answered. ``plans.decisions.answer``
  is its only writer and ``answered`` is terminal (``MUTABLE_STATES``), so the
  record is the decision identity's body: ``state``, ``answer``, ``answered_at``.
- ``ask_verdict``: the person's verdict on a review ask. Its record is the
  ``hold_cleared`` event that ``user_ask_release`` appends with provenance
  ``user.ask_cleared``; a person's answer carries ``detail.verdict``
  (``doceditor.review_bar``). A retired ask or an agent's route-back carries no
  verdict, so it is no call of the person's and is left out, as in
  ``gate_handoff_record._answers``.

Scope is the thread's own plans and those of every thread that delegated to it
(``threads.spawned_by`` upward), because a worker writes under its parent's
calls. The read returns every row: how many a caller can afford is the caller's
budget, not this read's.
"""

from __future__ import annotations

import json

from plans.decision_fields import chosen_ids
from plans.decision_projection import DECISION_TYPE

_ASK_ANSWERED_BY_PERSON = "user.ask_cleared"

#: The thread and, when asked, every thread above it by ``spawned_by``. UNION
#: (not UNION ALL) ends the walk on a cycle.
_CHAIN = """
WITH RECURSIVE chain(id) AS (
    SELECT ?
    UNION
    SELECT t.spawned_by FROM chain CROSS JOIN threads t
     WHERE t.id = chain.id AND t.spawned_by IS NOT NULL AND ?
),
scope(plan_id, thread_id) AS (
    SELECT p.id, p.owner_id FROM chain CROSS JOIN plans p
     WHERE p.owner_kind = 'thread' AND p.owner_id = chain.id
)
"""

_DECISIONS = _CHAIN + """
SELECT scope.thread_id, i.external_id, i.metadata
  FROM scope
  CROSS JOIN plan_nodes n ON n.plan_id = scope.plan_id
  CROSS JOIN plan_node_artifact_links l ON l.node_id = n.id AND l.role = 'attention'
  CROSS JOIN artifact_identities i ON i.id = l.artifact_id
 WHERE i.integration = 'local' AND i.type = ?
"""

_ASK_VERDICTS = _CHAIN + """
SELECT scope.thread_id, e.payload_json, e.created_at, h.owner_id AS artifact_id,
       h.reason, i.title AS artifact_title
  FROM scope
  CROSS JOIN plan_nodes n ON n.plan_id = scope.plan_id
  CROSS JOIN plan_node_events e INDEXED BY idx_plan_node_events_node ON e.node_id = n.id
  JOIN plan_node_holds h ON h.id = json_extract(e.payload_json, '$.hold_id')
  LEFT JOIN artifact_identities i ON i.id = h.owner_id
 WHERE e.event = 'hold_cleared' AND e.provenance = ?
"""


def read(db, thread_id: str, *, include_delegating_chain: bool) -> list[dict]:
    """Return the thread's settled calls, newest first.

    Each row: ``kind`` (``decision`` or ``ask_verdict``), ``id`` (the decision
    id, or the reviewed artifact's id), ``question``, ``answer``, ``note``,
    ``answered_at`` and ``thread_id`` (whose plan the call sat on).
    """
    chain = 1 if include_delegating_chain else 0
    rows = [
        *_decisions(db.execute(_DECISIONS, (thread_id, chain, DECISION_TYPE))),
        *_ask_verdicts(db.execute(_ASK_VERDICTS, (thread_id, chain, _ASK_ANSWERED_BY_PERSON))),
    ]
    rows.sort(key=lambda row: (row["answered_at"], row["id"]), reverse=True)
    return rows


def _decisions(cursor) -> list[dict]:
    rows = []
    for row in cursor:
        body = _json(row["metadata"]).get("plan_decision")
        if not isinstance(body, dict) or body.get("state") != "answered":
            continue
        stored = body.get("answer")
        answer: dict = stored if isinstance(stored, dict) else {}
        labels = {
            option.get("id"): option.get("label")
            for option in body.get("options") or [] if isinstance(option, dict)
        }
        chosen = [str(labels.get(option_id) or option_id) for option_id in chosen_ids(body, answer)]
        rows.append({
            "kind": "decision",
            "id": str(row["external_id"]),
            "question": str(body.get("title") or ""),
            "answer": "; ".join(chosen) or None,
            "note": answer.get("text") or None,
            "answered_at": str(body.get("answered_at") or ""),
            "thread_id": str(row["thread_id"]),
        })
    return rows


def _ask_verdicts(cursor) -> list[dict]:
    rows = []
    for row in cursor:
        detail = _json(row["payload_json"]).get("detail")
        if not isinstance(detail, dict) or not isinstance(detail.get("verdict"), str):
            continue
        verdict = detail["verdict"]
        rows.append({
            "kind": "ask_verdict",
            "id": str(row["artifact_id"] or ""),
            "question": str(row["artifact_title"] or row["reason"] or ""),
            "answer": verdict,
            "note": detail.get("note") or None,
            "answered_at": str(row["created_at"]),
            "thread_id": str(row["thread_id"]),
        })
    return rows


def _json(value) -> dict:
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
