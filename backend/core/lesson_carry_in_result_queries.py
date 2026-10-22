"""Read settled carry-in outcomes without writing or scheduling them."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.lesson_carry_in import CarryInFact


def prior_case_status(db, fact: CarryInFact, case_sha: str | None) -> str:
    """Return proven only after an earlier matching settled pass, in any run role."""
    if case_sha is None:
        return "unproven"
    from core.lesson_carry_in import RESULT_EVENT

    row = db.execute(
        """SELECT 1 FROM outcome_events
           WHERE event_name=? AND retracted_at IS NULL
             AND json_extract(attributes, '$.lesson_id')=?
             AND json_extract(attributes, '$.target_path')=?
             AND json_extract(attributes, '$.case_sha')=?
             AND json_extract(attributes, '$.verdict')='pass'
           LIMIT 1""",
        (RESULT_EVENT, fact.lesson_id, fact.target_path, case_sha),
    ).fetchone()
    return "proven" if row is not None else "unproven"


def result_event(db, fact: CarryInFact):
    """Find this carry-in fact's unretracted settled result event."""
    return db.execute(
        "SELECT id,ts FROM outcome_events WHERE dedupe_key=? AND retracted_at IS NULL",
        (f"{fact.subject_key}:result",),
    ).fetchone()


def baseline_result(db, fact: CarryInFact) -> tuple[CarryInFact, str] | None:
    """Return the newest settled fact used to compare the current model."""
    from core.lesson_carry_in import RESULT_EVENT

    rows = db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name=? AND retracted_at IS NULL ORDER BY ts DESC, rowid DESC",
        (RESULT_EVENT,),
    ).fetchall()
    for row in rows:
        try:
            attrs = json.loads(row["attributes"])
            if attrs.get("lesson_id") != fact.lesson_id or attrs.get("target_path") != fact.target_path:
                continue
            identity = attrs.get("model_identity")
            if not isinstance(identity, str) or not identity:
                continue
            previous = type(fact)(
                fact.lesson_id, fact.owner_thread_id, str(attrs["commit"]), fact.target_path,
                fact.case_ref, str(attrs.get("role", "edit")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        return previous, identity
    return None
