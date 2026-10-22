"""``run_script`` rules: a matching event queues one run of its writer's script.

Evaluation runs inside the event's write transaction, and a script run is
async and capacity-limited, so the rule's row in ``event_route_decisions`` is
the run's durable queue. Evaluation records it ``queued`` with the run's input,
or ``skipped: queue full`` once its rule already has ``QUEUE_CAP`` runs waiting;
the script runner claims it (``running``, input cleared) and settles it
(``ran`` or ``failed``, the failure's text as the reason). ``explain`` and
``list`` read that same row. A run the backend lost before it settled ends
``failed`` and is never replayed. A run that can't start (a paused writer, a
rule turned off, a script or owner gone) is ``skipped`` with the reason when it
would start, and nothing replays on resume.

The loop guard: an event a rule-started run sends, as a printed line or through
``corsia event emit`` (``events.trace_env``), carries the ids of every rule in its causal chain (``provenance['rule_trace']``, read into
``RuleEvent.trace``), and a rule already in that chain records ``skipped:
loop`` instead of running again. No depth limit: distinct rules chain freely.
Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from events import rules
from events.rule_outcomes import DROPPED, FAILED, QUEUED, RAN, RUNNING, SKIPPED, Outcome

OWNER_PAUSED = "owner paused"
LOOP = "loop"
QUEUE_FULL = "queue full"
#: Runs one rule may have waiting; a match past it is skipped, so a looping script stays bounded.
QUEUE_CAP = 50
INTERRUPTED = "the run stopped before it settled; it is not replayed"
TURNED_OFF = "the rule was turned off before its run started"
_REASON_LIMIT = 500


def check_script(db: sqlite3.Connection, writer: str, script_id: Any, extra: Mapping[str, Any]) -> str:
    """The script a ``run_script`` rule may run: one of its writer's own active script
    schedules. ``extra`` are the fields that belong to other actions, all refused."""
    from events.rule_authoring import RuleRefused

    given = sorted(name for name, value in extra.items() if value is not None)
    if given:
        raise RuleRefused(f"A run_script rule takes no {' or '.join(given)}: it runs your script, "
                          "and the event still reaches whoever it reaches today.")
    if not isinstance(script_id, str) or not script_id:
        raise RuleRefused("run_script needs script_id, the id automation_list shows for one of your "
                          "script schedules.")
    row = db.execute("SELECT thread_id, state FROM automation_schedules WHERE id=? AND kind='script'",
                     (script_id,)).fetchone()
    if row is None or row["thread_id"] != writer:
        raise RuleRefused(f"{script_id} isn't one of your script schedules; automation_list shows "
                          "yours, and automation_schedule registers one.")
    if row["state"] != "active":
        raise RuleRefused(f"Your script schedule {script_id} is {row['state']}; register the script "
                          "again with automation_schedule and use the new id.")
    return script_id


def _waiting(db: sqlite3.Connection, rule_id: str, writer: str) -> int:
    """The rule's queued runs, at most ``QUEUE_CAP`` read (``idx_event_route_decisions_rule``)."""
    return db.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM event_route_decisions "
        "WHERE rule_id=? AND thread_id=? AND outcome=? LIMIT ?)",
        (rule_id, writer, QUEUED, QUEUE_CAP),
    ).fetchone()[0]


def queue(
    db: sqlite3.Connection, event: rules.RuleEvent, matched: Iterable[rules.Rule],
    dropped_by: Mapping[str, str],
) -> list[Outcome]:
    """Each matching ``run_script`` rule's outcome: queued with its input, dropped by
    its writer's own drop, or skipped because it is already in the event's chain or
    its queue is full."""
    outcomes = []
    for rule in (rule for rule in matched if rule.action == rules.RUN_SCRIPT):
        writer = rule.created_by
        if writer in dropped_by:
            outcomes.append(Outcome(rule.id, writer, DROPPED,
                                    reason=f"dropped by rule {dropped_by[writer]}"))
        elif rule.id in event.trace:
            outcomes.append(Outcome(rule.id, writer, SKIPPED, reason=LOOP))
        elif _waiting(db, rule.id, writer) >= QUEUE_CAP:
            outcomes.append(Outcome(rule.id, writer, SKIPPED, reason=QUEUE_FULL))
        else:
            outcomes.append(Outcome(rule.id, writer, QUEUED, run_input=json.dumps({
                "event": {"type": f"{event.connector}.{event.event_type}", "id": event.event_id,
                          "rule_id": rule.id, "facts": dict(event.facts)},
                "trace": [*event.trace, rule.id],
            }, ensure_ascii=False, default=str)))
    return outcomes


def queued(db: sqlite3.Connection, limit: int) -> list[Mapping[str, Any]]:
    """Up to ``limit`` queued runs, oldest first, whose script has no run in flight, with
    their rule's script and whether it is still on. No run's input is read here."""
    if limit <= 0:
        return []
    return db.execute(
        "SELECT d.id, d.rule_id, d.thread_id, r.script_id, r.enabled "
        "FROM event_route_decisions d LEFT JOIN event_rules r ON r.id=d.rule_id "
        "LEFT JOIN automation_schedules s ON s.id=r.script_id "
        "WHERE d.outcome=? AND s.running_since IS NULL ORDER BY d.id LIMIT ?", (QUEUED, limit),
    ).fetchall()


def start(db: sqlite3.Connection, decision_id: int) -> dict[str, Any] | None:
    """Claim one queued run exactly once and return its input, which leaves storage with
    the claim; ``None`` if it was no longer queued."""
    row = db.execute("SELECT run_input FROM event_route_decisions WHERE id=? AND outcome=?",
                     (decision_id, QUEUED)).fetchone()
    if row is None or db.execute(
        "UPDATE event_route_decisions SET outcome=?, run_input=NULL WHERE id=? AND outcome=?",
        (RUNNING, decision_id, QUEUED),
    ).rowcount != 1:
        return None
    return json.loads(row["run_input"] or "{}")


def finish(db: sqlite3.Connection, decision_id: int, *, failed: str | None = None,
           skipped: str | None = None) -> None:
    """Settle one queued or running run: ran, failed (the reason says why) or skipped."""
    outcome, reason = (SKIPPED, skipped) if skipped else (FAILED, failed) if failed else (RAN, None)
    db.execute(
        "UPDATE event_route_decisions SET outcome=?, reason=?, run_input=NULL "
        "WHERE id=? AND outcome IN (?, ?)",
        (outcome, reason[:_REASON_LIMIT] if reason else None, decision_id, QUEUED, RUNNING),
    )


def interrupt(db: sqlite3.Connection, live: set[int]) -> None:
    """End each run its row says is in flight but no task of this process holds."""
    for row in db.execute("SELECT id FROM event_route_decisions WHERE outcome=?", (RUNNING,)).fetchall():
        if row["id"] not in live:
            finish(db, row["id"], failed=INTERRUPTED)
