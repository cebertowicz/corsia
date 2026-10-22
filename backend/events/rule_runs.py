"""``run_script`` rules: a matching event queues one run of its writer's script.

Evaluation runs inside the event's write transaction, and a script run is
async and capacity-limited, so the rule's row in ``event_route_decisions`` is
the run's durable queue. Evaluation records it ``queued`` with the run's input;
the script runner claims it (``running``, input cleared) and settles it
(``ran`` or ``failed``, the failure's text as the reason). ``explain`` and
``list`` read that same row. A run the backend lost before it settled ends
``failed`` and is never replayed; a paused writer's run is ``skipped`` when it
would start, and nothing replays on resume.

The loop guard: an event a rule-started run sends carries the ids of every
rule in its causal chain (``provenance['rule_trace']``, read into
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


def queue(
    event: rules.RuleEvent, matched: Iterable[rules.Rule], dropped_by: Mapping[str, str],
) -> list[Outcome]:
    """Each matching ``run_script`` rule's outcome: queued with its input, dropped by
    its writer's own drop, or skipped because it is already in the event's chain."""
    outcomes = []
    for rule in (rule for rule in matched if rule.action == rules.RUN_SCRIPT):
        writer = rule.created_by
        if writer in dropped_by:
            outcomes.append(Outcome(rule.id, writer, DROPPED,
                                    reason=f"dropped by rule {dropped_by[writer]}"))
        elif rule.id in event.trace:
            outcomes.append(Outcome(rule.id, writer, SKIPPED, reason=LOOP))
        else:
            outcomes.append(Outcome(rule.id, writer, QUEUED, run_input=json.dumps({
                "event": {"type": f"{event.connector}.{event.event_type}", "id": event.event_id,
                          "rule_id": rule.id, "facts": dict(event.facts)},
                "trace": [*event.trace, rule.id],
            }, ensure_ascii=False, default=str)))
    return outcomes


def queued(db: sqlite3.Connection) -> list[Mapping[str, Any]]:
    """Queued runs, oldest first, with their rule's script and whether it is still on."""
    return db.execute(
        "SELECT d.id, d.rule_id, d.thread_id, d.run_input, r.script_id, r.enabled "
        "FROM event_route_decisions d LEFT JOIN event_rules r ON r.id=d.rule_id "
        "WHERE d.outcome=? ORDER BY d.id", (QUEUED,),
    ).fetchall()


def start(db: sqlite3.Connection, decision_id: int) -> bool:
    """Claim one queued run exactly once; its input leaves storage with the claim."""
    return db.execute(
        "UPDATE event_route_decisions SET outcome=?, run_input=NULL WHERE id=? AND outcome=?",
        (RUNNING, decision_id, QUEUED),
    ).rowcount == 1


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
