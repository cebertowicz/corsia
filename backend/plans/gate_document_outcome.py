"""Whether a settled gate run's document went ahead as the person first saw it, read once and kept.

The answer comes from the workspace history (``infra.document_went_ahead``), which runs Git and takes
seconds on a long history, so it is never read inside a write: the gate's settlement registers the
read for after its commit, and the answer is kept as one event on the run's phase. Replaying a gate
reads the kept answers, never Git, so a replay is quick and gives the same answer every time (the
history reader also looks at the working file, which keeps changing after the run settled).

A read Git can't answer is kept as a fault: that run counts neither for nor against the gate.
"""

from __future__ import annotations

import json
import logging

import plans.service as service

DOCUMENT_EVENT = "gate_document_read"
PROVENANCE = "plan.gate_document"

log = logging.getLogger(__name__)


def kept(db, phases) -> dict[str, dict]:
    """Each phase's kept read, ``{"outcome": {...} | None}`` or ``{"fault": text}``, in one statement."""
    return {
        row["node_id"]: json.loads(row["payload_json"] or "{}")
        for row in db.execute(
            "SELECT node_id,payload_json FROM plan_node_events WHERE event=?"
            " AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,rowid",
            (DOCUMENT_EVENT, json.dumps(list(phases))),
        )
    }


def read(db, artifact_id: str, first_round_at: str, as_of: str) -> dict:
    """One run's read from the workspace history; runs Git, so call it with no write open."""
    from infra.document_went_ahead import went_ahead_outcomes
    from infra.guidance_states import GitFault

    run = (artifact_id, first_round_at, as_of)
    try:
        return {"outcome": went_ahead_outcomes([run], db=db)[run]}
    except GitFault as fault:
        log.warning("gate document read for %s failed; the run counts neither way: %s", artifact_id, fault)
        return {"fault": str(fault) or "history unavailable"}


def keep(db, phase_id: str, answer: dict) -> None:
    """Keep a run's read on its phase, once: a second reader of the same settlement keeps nothing."""
    if phase_id not in kept(db, [phase_id]):
        service._event(db, phase_id, DOCUMENT_EVENT, actor_id=None, provenance=PROVENANCE, payload=answer)
