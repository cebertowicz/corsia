"""One rule outcome row as ``explain`` shows it: what the rule did, and why. Names no connector."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from events import rule_outcomes as outcomes

_TEXT = {
    outcomes.WOKE: "rule {rule} woke {thread}",
    outcomes.DIGESTED: "rule {rule} put it in {thread}'s digest",
    outcomes.DROPPED: "rule {rule} dropped it for {thread}",
    outcomes.HELD: "rule {rule} held it from {thread}",
    outcomes.DEAD_LETTERED: "rule {rule} could not deliver it",
    outcomes.ENDED: "rule {rule} did not fire: the file had already ended",
    outcomes.DUPLICATE: "rule {rule}: {thread} already had it",
    outcomes.REGISTERED: "rule {rule} registered the file the command's output named to {thread}",
    outcomes.NOT_REGISTERED: "rule {rule} did not register to {thread}",
}
# A script or worker rule delivers nothing, so what it did is told in its own words. A
# script that never started (refused at its claim, or its owner gone at evaluation) reads
# "did not run"; "failed" is only a run that started.
_ACTION_TEXT = {
    outcomes.RUN_SCRIPT: {
        outcomes.QUEUED: "rule {rule} will run {thread}'s script with it",
        outcomes.RUNNING: "rule {rule} is running {thread}'s script with it",
        outcomes.RAN: "rule {rule} ran {thread}'s script with it",
        outcomes.FAILED: "rule {rule}'s run of {thread}'s script with it failed",
        outcomes.SKIPPED: "rule {rule} did not run {thread}'s script",
        outcomes.DEAD_LETTERED: "rule {rule} did not run {thread}'s script",
    },
    outcomes.START_WORKER: {
        outcomes.QUEUED: "rule {rule} will start a worker for {thread} with it",
        outcomes.STARTED: "rule {rule} started a worker for {thread}",
        outcomes.RETURNED: "rule {rule}'s worker for {thread} returned a result",
        outcomes.INVALID: "rule {rule}'s worker for {thread} returned a result its workflow refuses",
        outcomes.NO_RESULT: "rule {rule}'s worker for {thread} returned nothing",
        outcomes.REFUSED: "rule {rule} did not start a worker for {thread}",
        outcomes.SKIPPED: "rule {rule} did not start a worker for {thread}",
        outcomes.DEAD_LETTERED: "rule {rule} did not start a worker for {thread}",
    },
}


def decision(db: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    """The shaped row; a drop also names any armed watch it left waiting."""
    outcome, reason, thread = row["outcome"], row["reason"], row["thread_id"]
    texts = _ACTION_TEXT.get(row["action"], {})
    template = texts.get(outcome) or _TEXT[outcome]
    if outcome == outcomes.NOT_REGISTERED and row["artifact_external_id"]:
        template = "rule {rule} did not register the file the command's output named to {thread}"
    text = template.format(rule=row["rule_id"], thread=thread)
    if row["via"] == "routed" and outcome in (outcomes.WOKE, outcomes.DIGESTED):
        text += " (with the delivery routing already made)"
    if reason:
        text += f": {reason}"
    shaped: dict[str, Any] = {
        "at": row["at"], "event_id": row["event_id"], "event_type": row["event_type"],
        "artifact": row["artifact_external_id"], "rule_id": row["rule_id"], "thread_id": thread,
        "outcome": outcome, "reason": reason, "explanation": text,
    }
    if outcome == outcomes.DROPPED and thread and row["artifact_external_id"]:
        watches = [w["id"] for w in db.execute(
            "SELECT id FROM expected_event_watches WHERE artifact_integration=? AND artifact_type=? "
            "AND artifact_external_id=? AND expected_event=? AND owner_thread_id=? AND status='armed'",
            (row["artifact_integration"], row["artifact_type"], row["artifact_external_id"],
             row["watch_event"], thread),
        ).fetchall()]
        if watches:
            shaped["armed_watches_left_waiting"] = watches
            shaped["explanation"] += (
                f"; armed watch {', '.join(watches)} still waits, since a dropped event doesn't end it")
    return shaped
