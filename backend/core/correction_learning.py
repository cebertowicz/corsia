"""Immutable correction triage and terminal-outcome facts for the learning loop."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

TRIAGE_EVENT = "learning_loop.correction_triage"
OUTCOME_EVENT = "learning_loop.correction_outcome"
TIERS = ("not_correction", "instruction", "note", "miss", "serious")
CAUSES = (
    "wrong_rule", "no_foundation", "rule_gap", "process_gap", "not_delivered",
    "not_followed", "cant_be_done",
)
NEW_WORK_SIGNALS = ("task_kind", "agent", "workflow_version")
TERMINAL_OUTCOMES = ("fixed", "instruction", "watch", "lesson", "workflow", "dropped")
SERIOUS_TOOL_NAMES = ("email_send", "calendar_send_invite", "git_push", "github_pr_merge")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def loaded_targets(db: sqlite3.Connection, thread_id: str) -> tuple[str, ...]:
    """Return distinct file paths delivered or read in the active turn.

    Manifest entries retain stable references rather than prompt text; the read
    ledger adds on-demand files. Both are evidence that the agent saw a file.
    """
    turn = db.execute(
        """SELECT id,context_manifest_id FROM active_turns
           WHERE thread_id=? AND status='running' ORDER BY started_at DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()
    if turn is None:
        return ()
    found: list[str] = []
    manifest_id = turn["context_manifest_id"]
    if manifest_id:
        row = db.execute("SELECT entries_json FROM context_manifests WHERE id=?", (manifest_id,)).fetchone()
        try:
            entries = json.loads(row["entries_json"]) if row else []
        except (TypeError, ValueError):
            entries = []
        for entry in entries if isinstance(entries, list) else []:
            reference = entry.get("stable_reference") if isinstance(entry, dict) else None
            if isinstance(reference, str) and "/" in reference and reference not in found:
                found.append(reference)
    for row in db.execute("SELECT path FROM guidance_loads WHERE turn_id=? ORDER BY created_at,id", (turn["id"],)):
        path = str(row["path"])
        if path not in found:
            found.append(path)
    return tuple(found)


def serious_proposed(db: sqlite3.Connection, thread_id: str) -> bool:
    """Detect the durable, named tool effects that require Serious by default."""
    marks = ",".join("?" for _ in SERIOUS_TOOL_NAMES)
    row = db.execute(
        f"""SELECT 1 FROM messages WHERE thread_id=? AND role='tool'
            AND active_turn_id=(SELECT id FROM active_turns WHERE thread_id=? AND status='running')
            AND json_extract(metadata, '$.name') IN ({marks}) LIMIT 1""",
        (thread_id, thread_id, *SERIOUS_TOOL_NAMES),
    ).fetchone()
    return row is not None


def _require(value: object, choices: tuple[str, ...], field: str) -> str:
    if value not in choices:
        raise ValueError(f"{field} must be one of: {', '.join(choices)}")
    return str(value)


def _subject_exists(db: sqlite3.Connection, subject_key: str) -> None:
    from core.corrections_recording import CORRECTION_EVENT

    row = db.execute(
        "SELECT 1 FROM outcome_events WHERE event_name=? AND subject_key=? LIMIT 1",
        (CORRECTION_EVENT, subject_key),
    ).fetchone()
    if row is None:
        raise ValueError("subject_key is not a recorded correction for this thread")


def record_triage(
    db: sqlite3.Connection, *, thread_id: str, subject_key: str, tier: object, cause: object,
    new_work: object, new_work_signal: object, target_file: object,
    serious_lowered_reason: object = None,
) -> dict[str, Any]:
    """Write the one immutable, receipt-backed triage fact for a correction."""
    tier_value = _require(tier, TIERS, "tier")
    cause_value = _require(cause, CAUSES, "cause")
    if new_work not in ("yes", "no"):
        raise ValueError("new_work must be yes or no")
    if new_work == "yes":
        signal = _require(new_work_signal, NEW_WORK_SIGNALS, "new_work_signal")
    elif new_work_signal is not None:
        raise ValueError("new_work_signal is only allowed when new_work is yes")
    else:
        signal = None
    if not isinstance(target_file, str) or not target_file.strip():
        raise ValueError("target_file is required")
    target = target_file.strip()
    valid_targets = loaded_targets(db, thread_id)
    if target not in valid_targets:
        choices = ", ".join(valid_targets) or "(no manifest or load-receipt files recorded)"
        raise ValueError(f"target_file is not a file loaded for this turn. Choose one of: {choices}.")
    proposed_serious = serious_proposed(db, thread_id)
    lowered = serious_lowered_reason.strip() if isinstance(serious_lowered_reason, str) else None
    if serious_lowered_reason is not None and not lowered:
        raise ValueError("serious_lowered_reason must state why Serious was lowered")
    if tier_value == "serious" and lowered:
        raise ValueError("serious_lowered_reason is only allowed when tier is not serious")
    if proposed_serious and tier_value != "serious" and not lowered:
        raise ValueError("Serious is proposed because this task took an outside or hard-to-undo tool effect; serious_lowered_reason is required to lower it")
    _subject_exists(db, subject_key)
    existing = db.execute(
        "SELECT 1 FROM outcome_events WHERE event_name=? AND subject_key=? LIMIT 1",
        (TRIAGE_EVENT, subject_key),
    ).fetchone()
    if existing:
        raise ValueError("a triage fact already exists for this correction subject")
    from infra.outcome_events import record_outcome_event

    result = record_outcome_event(
        db, dedupe_key=f"{TRIAGE_EVENT}:{subject_key}", event_name=TRIAGE_EVENT,
        source="agent", provenance="agent_recorded", ts=_now(), subject_key=subject_key,
        agent_thread_id=thread_id, attributes={
            "tier": tier_value, "cause": cause_value, "new_work": str(new_work),
            "new_work_signal": signal, "target_file": target,
            "serious_proposed": "yes" if proposed_serious else "no", "serious_lowered_reason": lowered,
        },
    )
    return {"subject_key": subject_key, "event_id": result.event_id, "target_file": target}


def record_terminal_outcome(
    db: sqlite3.Connection, *, thread_id: str, subject_key: str, outcome: object,
    reason: object = None, lesson_id: object = None, workflow_version: object = None,
) -> dict[str, Any]:
    """Write exactly one terminal correction outcome; dropped needs a reason."""
    outcome_value = _require(outcome, TERMINAL_OUTCOMES, "outcome")
    clean_reason = reason.strip() if isinstance(reason, str) else None
    if outcome_value == "dropped" and not clean_reason:
        raise ValueError("dropped outcome requires reason")
    if reason is not None and not clean_reason:
        raise ValueError("reason must be non-empty when supplied")
    for field, value in (("lesson_id", lesson_id), ("workflow_version", workflow_version)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{field} must be a non-empty string when supplied")
    _subject_exists(db, subject_key)
    existing = db.execute(
        "SELECT 1 FROM outcome_events WHERE event_name=? AND subject_key=? LIMIT 1",
        (OUTCOME_EVENT, subject_key),
    ).fetchone()
    if existing:
        raise ValueError("a terminal outcome already exists for this correction subject")
    from infra.outcome_events import record_outcome_event

    result = record_outcome_event(
        db, dedupe_key=f"{OUTCOME_EVENT}:{subject_key}", event_name=OUTCOME_EVENT,
        source="agent", provenance="agent_recorded", ts=_now(), subject_key=subject_key,
        agent_thread_id=thread_id, attributes={
            "outcome": outcome_value, "reason": clean_reason,
            "lesson_id": lesson_id.strip() if isinstance(lesson_id, str) else None,
            "workflow_version": workflow_version.strip() if isinstance(workflow_version, str) else None,
        },
    )
    return {"subject_key": subject_key, "event_id": result.event_id, "outcome": outcome_value}
