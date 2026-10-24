"""The lesson gate's one shape: four stated fields at ``place`` time, and the facts it counts.

Deterministic and unjudged: each field is checked only for being plain text, an
optional ``widens`` id only for existing, and a person's message only by
finding it in the caller's project scope. A refusal is built from
that result, naming the fields it found missing and one wrong/right pair; the
same definitions are the tool's own parameter text. Every refusal and every
accepted lesson is one ``measurement.lesson_gate`` outcome event (no new store),
which the learning status report counts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

EVENT = "measurement.lesson_gate"
FIELDS = ("decision", "rule", "second_case", "not_when")
SAVE_REASON = "lesson-outside-placement"
_DECISION_CHARS = 300

DEFINITIONS = {
    "decision": "the kind of choice that went wrong, naming no single check or step.",
    "rule": "what to do at that choice from now on, for any case of it.",
    "second_case": "different work where this rule would also have prevented a miss.",
    "not_when": "when the rule does not apply.",
    "widens": "optional: entry_id of your earlier lesson this widens; it must exist.",
    "message_id": "optional: id of a person-written message in this project; its exact words skip the four fields (a proposal needs no exact match).",
}
#: The ``place`` parameters these definitions describe, as tool schema.
PARAMETERS = {
    name: ({"type": "integer", "minimum": 1} if name == "widens" else {"type": "string"}) | {"description": text}
    for name, text in DEFINITIONS.items()
}
WRONG_RIGHT = (
    'Wrong decision (one check): "read the stamp on this frame". '
    'Right (the choice it belongs to): "accepting evidence that work is done".'
)
_LINE_VERDICTS = ("written",)


@dataclass(frozen=True)
class Gate:
    """One ``place`` call, checked. ``refusal`` is the reply when it is not accepted."""

    refusal: str | None = None
    reason: str | None = None
    problems: tuple[str, ...] = ()
    decision: str | None = None
    widens: int | None = None


def _plain(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _line(params: dict[str, Any]) -> str:
    placement = params.get("placement")
    text = placement.get("text") if isinstance(placement, dict) else None
    return str(text or params.get("quote") or "")


def _person_said(db: sqlite3.Connection, thread_id: str, message_id: Any, line: str | None) -> bool:
    """Whether a project-scope person message is the requested source, and optionally says ``line``."""
    from core.corrections_recording import is_owner_authored_message
    from core.placement_decide import says

    if not isinstance(message_id, str) or not message_id:
        return False
    row = db.execute(
        "SELECT m.role AS role,m.content AS content,m.metadata AS metadata FROM messages AS m "
        "JOIN threads AS source ON source.id=m.thread_id "
        "JOIN threads AS caller ON caller.id=? WHERE m.id=? AND "
        "((caller.project_id IS NULL AND source.id=caller.id) OR "
        "(caller.project_id IS NOT NULL AND source.project_id=caller.project_id))",
        (thread_id, message_id),
    ).fetchone()
    return row is not None and is_owner_authored_message(
        role=row["role"], content=row["content"], metadata=row["metadata"],
    ) and (line is None or says(row["content"], line))


def _requires_fields(db: sqlite3.Connection, thread_id: str, params: dict[str, Any]) -> bool:
    """Whether a mapped placement writes a lesson rather than a proposal, fact, or reference."""
    verdict = str(params["verdict"])
    placement = params.get("placement")
    if not isinstance(placement, dict):
        return verdict == "written"
    from core.placement_answer import thread_place
    from core.placement_decide import InvalidDecision, map_destination, validate_decision

    try:
        return map_destination(thread_place(db, thread_id), validate_decision(placement)).kind in {
            "context", "skill", "trial",
        }
    except InvalidDecision:
        return verdict != "proposed"


def _widens_exists(db: sqlite3.Connection, thread_id: str, epoch: int, entry_id: int, widens: Any) -> bool:
    if isinstance(widens, bool) or not isinstance(widens, int) or widens < 1 or widens == entry_id:
        return False
    return db.execute(
        "SELECT 1 FROM continuity_entries WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (thread_id, epoch, widens),
    ).fetchone() is not None


def check_place(db: sqlite3.Connection, thread_id: str, epoch: int, params: dict[str, Any]) -> Gate:
    """Check one in-turn ``place``: fields only for a lesson destination, with person sources checked."""
    verdict = str(params["verdict"])
    if verdict == "already":
        return Gate()
    problems: list[str] = []
    message_id = params.get("message_id")
    requires_fields = _requires_fields(db, thread_id, params)
    verbatim = (
        requires_fields and verdict in _LINE_VERDICTS and message_id is not None
        and _person_said(db, thread_id, message_id, _line(params))
    )
    person_proposal = verdict == "proposed" and message_id is not None and _person_said(
        db, thread_id, message_id, None,
    )
    if requires_fields and not verbatim:
        problems += [name for name in FIELDS if not _plain(params.get(name))]
    if message_id is not None and not (verbatim or person_proposal):
        problems.append("message_id")
    widens = params.get("widens")
    if widens is not None and not _widens_exists(db, thread_id, epoch, int(params["entry_id"]), widens):
        problems.append("widens")
    if problems:
        reason = "widens" if problems == ["widens"] else "fields"
        return Gate(refusal_text(problems), reason, tuple(problems))
    decision = params.get("decision")
    return Gate(
        reason="verbatim" if verbatim else "fields",
        decision=" ".join(str(decision).split())[:_DECISION_CHARS] if _plain(decision) else None,
        widens=widens,
    )


def refusal_text(problems: list[str]) -> str:
    """The reply to a refused ``place``: a distinct cause and definition for each problem."""
    fields = [name for name in problems if name in FIELDS]
    lines = []
    if fields:
        names = ", ".join(fields)
        lines.append(f"Error: place not accepted: {names} {'is' if len(fields) == 1 else 'are'} missing or not plain text.")
    else:
        lines.append("Error: place not accepted.")
    for name in problems:
        cause = (
            "missing or not plain text" if name in FIELDS else
            "no lesson with that id" if name == "widens" else
            "no person-written message in this project contains the line"
        )
        lines.append(f"- {name}: {cause}. {DEFINITIONS[name]}")
    return "\n".join([*lines, WRONG_RIGHT])


def save_refusal_line(path: Path, *, removed: bool = False) -> str:
    """The reply to a file save that grows lesson items outside ``place``."""
    lead = "The file is unchanged." if path.exists() else "Nothing was written."
    return "\n".join([
        f"Error: {path.name} not saved: a lesson is placed with continuity_edit place, which writes the line itself. {lead}",
        str(path),
        "Fix: record it as a continuity entry, then call continuity_edit with operation place, verdict written, "
        "entry_id, placement, decision, rule, second_case and not_when (or message_id for a person's exact words).",
        *( ["To merge lessons, save the file without the old lines, then place the merged one."] if removed else [] ),
        WRONG_RIGHT,
    ])


def record(
    db: sqlite3.Connection, thread_id: str | None, *, result: str, reason: str, verdict: str,
    fields: tuple[str, ...] = (), decision: str | None = None, widens: int | None = None,
    items_before: int | None = None, items_after: int | None = None,
) -> None:
    """One outcome fact on the caller's open transaction; nothing here commits."""
    from infra.outcome_events import record_outcome_event

    attributes: dict[str, Any] = {"result": result, "reason": reason, "verdict": verdict}
    if fields:
        attributes["fields"] = ",".join(fields)
    if decision:
        attributes["decision"] = decision
    if widens is not None:
        attributes["widens"] = str(widens)
    if items_before is not None:
        attributes["items_before"] = items_before
    if items_after is not None:
        attributes["items_after"] = items_after
    record_outcome_event(
        db, dedupe_key=f"lesson-gate:{uuid.uuid4()}", event_name=EVENT, source="corsia",
        provenance="instrumented", ts=datetime.now(timezone.utc).isoformat(),
        agent_thread_id=thread_id, attributes=attributes,
    )


def record_refusal(db: sqlite3.Connection, thread_id: str, verdict: str, gate: Gate) -> None:
    """Record a refused ``place`` in its own short transaction, after the check has read."""
    from infra.database import write_transaction

    with write_transaction(db, scope="lesson_gate") as connection:
        record(connection, thread_id, result="refused", reason=str(gate.reason), verdict=verdict, fields=gate.problems)


def record_accepted(db: sqlite3.Connection, thread_id: str, verdict: str, gate: Gate) -> None:
    """Record an accepted lesson on the transaction that queues it; ``already`` writes nothing new."""
    if gate.reason is not None:
        record(
            db, thread_id, result="accepted", reason=gate.reason, verdict=verdict,
            decision=gate.decision, widens=gate.widens,
        )


def record_save_observation(
    thread_id: str | None, *, reason: str, items_before: int, items_after: int,
) -> None:
    """Record one completed save observation, never an accepted placed lesson."""
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="lesson_gate") as connection:
            record(
                connection, thread_id, result="observed", reason=reason, verdict="save",
                items_before=items_before, items_after=items_after,
            )
    finally:
        db.close()


def record_save_refusal(author: str | None) -> str | None:
    """Record a refused file save, or return the visible metric-loss warning.

    The refusal remains authoritative when measurement is unavailable, but the
    caller must not mistake an unrecorded refusal for a counted one.
    """
    from infra.database import get_db, write_transaction
    from infra.guidance_messages import saver_thread

    try:
        db = get_db()
        try:
            with write_transaction(db, scope="lesson_gate") as connection:
                record(connection, saver_thread(author), result="refused", reason=SAVE_REASON, verdict="save")
        finally:
            db.close()
    except Exception:
        logger.opt(exception=True).warning("lesson gate refusal could not be recorded")
        return "Lesson gate measurement was not recorded. The save remains refused; report this."
    return None


def gate_counts(db: sqlite3.Connection, thread_ids: tuple[str, ...], from_ts: str, to_ts: str) -> dict[str, Any]:
    """Lesson-gate facts in ``[from_ts, to_ts)``, with save observations kept separate."""
    counts: dict[str, Any] = {"refused": 0, "accepted": 0, "widens": 0}
    if not thread_ids:
        return counts
    rows = db.execute(
        "SELECT json_extract(attributes,'$.result') AS result, json_extract(attributes,'$.reason') AS reason, "
        "count(*) AS n, sum(json_extract(attributes,'$.widens') IS NOT NULL) AS widens FROM outcome_events "
        "WHERE event_name=? AND retracted_at IS NULL AND ts>=? AND ts<? "
        "AND json_extract(attributes,'$.result') IN ('refused','accepted','observed') "
        "AND agent_thread_id IN (SELECT value FROM json_each(?)) GROUP BY result, reason",
        (EVENT, from_ts, to_ts, json.dumps(list(thread_ids))),
    ).fetchall()
    observations: dict[str, int] = {}
    for row in rows:
        result, reason = row["result"], row["reason"]
        if result in {"refused", "accepted"}:
            counts[result] += int(row["n"])
            counts["widens"] += int(row["widens"] or 0)
        elif result == "observed" and reason in {"rewording", "stewardship"}:
            observations[reason] = int(row["n"])
    if observations:
        counts["observed"] = observations
    return counts


def consolidation_facts(
    db: sqlite3.Connection, thread_ids: tuple[str, ...], from_ts: str, to_ts: str,
) -> list[str] | None:
    """Recorded consolidation event ids, or ``None`` before gate measurement begins.

    A consolidation is either a completed save that reduced its lesson-item count,
    or an accepted ``place`` that widened an existing lesson. The report reads this
    facade instead of restating the outcome-event rule.
    """
    if not thread_ids:
        return None
    first = db.execute(
        "SELECT MIN(ts) FROM outcome_events WHERE event_name=? AND retracted_at IS NULL "
        "AND agent_thread_id IN (SELECT value FROM json_each(?))",
        (EVENT, json.dumps(list(thread_ids))),
    ).fetchone()[0]
    if first is None or str(first) >= to_ts:
        return None
    rows = db.execute(
        "SELECT id FROM outcome_events WHERE event_name=? AND retracted_at IS NULL "
        "AND ts>=? AND ts<? AND agent_thread_id IN (SELECT value FROM json_each(?)) AND ("
        "(json_extract(attributes,'$.result')='observed' "
        "AND json_extract(attributes,'$.verdict')='save' "
        "AND json_extract(attributes,'$.items_after') < json_extract(attributes,'$.items_before')) "
        "OR (json_extract(attributes,'$.result')='accepted' "
        "AND json_extract(attributes,'$.widens') IS NOT NULL)) ORDER BY ts,id",
        (EVENT, from_ts, to_ts, json.dumps(list(thread_ids))),
    ).fetchall()
    return [str(row["id"]) for row in rows]
