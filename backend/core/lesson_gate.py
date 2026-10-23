"""The lesson gate's one shape: four stated fields at ``place`` time, and the facts it counts.

Deterministic and unjudged: each field is checked only for being plain text, an
optional ``widens`` id only for existing, and a person's verbatim instruction
only by finding its line in a message a person wrote. A refusal is built from
that result, naming the fields it found missing and one wrong/right pair; the
same definitions are the tool's own parameter text. Every refusal and every
accepted lesson is one ``measurement.lesson_gate`` outcome event (no new store),
which the learning status report counts.
"""

from __future__ import annotations

import json
import re
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
    "decision": "the kind of choice you were making when it went wrong, naming no single check or step.",
    "rule": "what to do at that choice from now on, stated for any case of it.",
    "second_case": "a different kind of work where this rule would also have prevented a miss.",
    "not_when": "when the rule does not apply.",
    "widens": "optional: the entry_id of an earlier lesson of yours that this one widens; it must exist.",
    "literal_ref": "optional: the id of a message a person wrote in this thread that contains the line exactly; skips the four fields.",
}
WRONG_RIGHT = (
    'Wrong decision (one check): "read the stamp on this frame". '
    'Right (the choice it belongs to): "accepting evidence that work is done".'
)
_LINE_VERDICTS = ("written", "proposed")
_AUTHOR_THREAD = re.compile(r"<([^<>@\s]+)@agents\.corsia\.local>\s*$")


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


def _person_said(db: sqlite3.Connection, thread_id: str, message_id: Any, line: str) -> bool:
    """Whether ``message_id`` is a message a person wrote in this thread that carries ``line``."""
    from core.corrections_recording import is_owner_authored_message
    from core.placement_decide import says

    if not isinstance(message_id, str) or not message_id:
        return False
    row = db.execute(
        "SELECT role,content,metadata FROM messages WHERE thread_id=? AND id=?", (thread_id, message_id),
    ).fetchone()
    return row is not None and is_owner_authored_message(
        role=row["role"], content=row["content"], metadata=row["metadata"],
    ) and says(row["content"], line)


def _widens_exists(db: sqlite3.Connection, thread_id: str, epoch: int, entry_id: int, widens: Any) -> bool:
    if isinstance(widens, bool) or not isinstance(widens, int) or widens < 1 or widens == entry_id:
        return False
    return db.execute(
        "SELECT 1 FROM continuity_entries WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (thread_id, epoch, widens),
    ).fetchone() is not None


def check_place(db: sqlite3.Connection, thread_id: str, epoch: int, params: dict[str, Any]) -> Gate:
    """Check one in-turn ``place``: the four fields, unless a person's words are resolved verbatim."""
    verdict = str(params["verdict"])
    if verdict == "already":
        return Gate()
    problems: list[str] = []
    verbatim = (
        verdict in _LINE_VERDICTS and params.get("literal_ref") is not None
        and _person_said(db, thread_id, params["literal_ref"], _line(params))
    )
    if not verbatim:
        problems += [name for name in FIELDS if not _plain(params.get(name))]
        if params.get("literal_ref") is not None and problems:
            problems.append("literal_ref")
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
    """The reply to a refused ``place``: what is missing, how each is defined, one pair."""
    names = ", ".join(problems)
    lines = [f"Error: place not accepted: {names} {'is' if len(problems) == 1 else 'are'} missing or not plain text."]
    lines += [f"- {name}: {DEFINITIONS[name]}" for name in problems]
    return "\n".join([*lines, WRONG_RIGHT])


def save_refusal_line(path: Path) -> str:
    """The reply to a file save that adds or changes a lesson outside ``place``."""
    lead = "The file is unchanged." if path.exists() else "Nothing was written."
    return "\n".join([
        f"Error: {path.name} not saved: a lesson is placed with continuity_edit place, which writes the line itself. {lead}",
        str(path),
        "Fix: record it as a continuity entry, then call continuity_edit with operation place, verdict written, "
        "entry_id, placement, decision, rule, second_case and not_when.",
        WRONG_RIGHT,
    ])


def author_thread(author: str | None) -> str | None:
    """The thread a guidance author names, or None for the person and CORSIA."""
    found = _AUTHOR_THREAD.search(author or "")
    return found.group(1) if found else None


def record(
    db: sqlite3.Connection, thread_id: str | None, *, result: str, reason: str, verdict: str,
    fields: tuple[str, ...] = (), decision: str | None = None, widens: int | None = None,
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


def record_save_refusal(author: str | None) -> None:
    """Record a refused file save. A failure to record is logged, never raised: the refusal stands."""
    from infra.database import get_db, write_transaction

    try:
        db = get_db()
        try:
            with write_transaction(db, scope="lesson_gate") as connection:
                record(connection, author_thread(author), result="refused", reason=SAVE_REASON, verdict="save")
        finally:
            db.close()
    except Exception:
        logger.opt(exception=True).warning("lesson gate refusal could not be recorded")


def gate_counts(db: sqlite3.Connection, thread_ids: tuple[str, ...], from_ts: str, to_ts: str) -> dict[str, int]:
    """Refusals, accepted lessons and widens links for these threads in ``[from_ts, to_ts)``."""
    counts = {"refused": 0, "accepted": 0, "widens": 0}
    if not thread_ids:
        return counts
    rows = db.execute(
        "SELECT json_extract(attributes,'$.result') AS result, count(*) AS n, "
        "sum(json_extract(attributes,'$.widens') IS NOT NULL) AS widens FROM outcome_events "
        "WHERE event_name=? AND retracted_at IS NULL AND ts>=? AND ts<? "
        "AND agent_thread_id IN (SELECT value FROM json_each(?)) GROUP BY result",
        (EVENT, from_ts, to_ts, json.dumps(list(thread_ids))),
    ).fetchall()
    for row in rows:
        if row["result"] in counts:
            counts[row["result"]] = int(row["n"])
            counts["widens"] += int(row["widens"] or 0)
    return counts
