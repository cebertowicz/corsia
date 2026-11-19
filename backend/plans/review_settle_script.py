"""A settle script's report settles its review step.

A review step with a settle name (creation payload ``workflow_settle``) is
settled only by the script started for it, never by its reviewer's own
verdict. The runner records a pending settle for one attempt before it starts
the script (``record_pending_settle``), binds the script's run to it
(``bind_settle_run``), and reports the run's verdict (``settle_step_from_script``).
Each is a step event, so the step's own record says what is pending and how it
ended. Retries, stuck and package-wide holds are not here.
"""

from __future__ import annotations

import json

import plans.service as service
from plans import review_settle

PENDING = "review_settle_pending"
BOUND = "review_settle_bound"
SETTLED = "review_settle_outcome"
NO_RESULT = "review_no_result"
#: The close reason of a review step whose work changed after its review read it.
STALE = "Sent back: the work changed after the review read it"
#: Outcomes that end an attempt: any other run for it is then ``already``.
_FINAL = frozenset({"accepted", "sent_back", "stale"})


def step_settle_script(connection, node_id: str) -> str | None:
    """The step's settle name, or None for a step its reviewer's verdict settles."""
    from plans.workflow_fields import creation_payload

    name = creation_payload(connection, node_id).get("workflow_settle")
    return name if isinstance(name, str) and name else None


def _events(connection, node_id: str, attempt_id: str, *names: str) -> list:
    return connection.execute(
        f"""SELECT rowid,event,payload_json FROM plan_node_events
            WHERE node_id=? AND event IN ({",".join("?" * len(names))})
              AND json_valid(payload_json) AND json_extract(payload_json,'$.attempt_id')=?
            ORDER BY rowid""",
        (node_id, *names, attempt_id),
    ).fetchall()


def _pending(connection, node_id: str, attempt_id: str) -> dict | None:
    """The attempt's pending settle, unless a no-result report has since cleared it."""
    rows = _events(connection, node_id, attempt_id, PENDING, NO_RESULT)
    if not rows or rows[-1]["event"] != PENDING:
        return None
    return json.loads(rows[-1]["payload_json"])


def record_pending_settle(
    connection, *, node_id: str, attempt_id: str, reviewer_thread_id: str, version: str,
) -> None:
    """Record that a settle script starts for this attempt, reviewing work at ``version``.

    The reviewer is held to the same rule as a reviewing worker: one that has
    seen the work can't settle the step (``fresh_reviewer.NOT_FRESH``).
    """
    from plans import fresh_reviewer

    with service._write(connection):
        step = service._live_node_row(connection, node_id)
        if fresh_reviewer.has_seen_the_work(connection, step, reviewer_thread_id):
            raise service.PlanError(fresh_reviewer.NOT_FRESH.format(worker=reviewer_thread_id))
        service._event(
            connection, node_id, PENDING, actor_id=None, provenance=review_settle.PROVENANCE,
            payload={"attempt_id": attempt_id, "reviewer_thread_id": reviewer_thread_id, "version": version},
        )


def bind_settle_run(connection, *, node_id: str, attempt_id: str, run_id: str) -> None:
    """Bind the started script's run to the attempt's pending settle."""
    with service._write(connection):
        if _pending(connection, node_id, attempt_id) is None:
            raise service.PlanError(f"no pending settle for attempt {attempt_id} on {node_id}")
        service._event(
            connection, node_id, BOUND, actor_id=None, provenance=review_settle.PROVENANCE,
            payload={"attempt_id": attempt_id, "run_id": run_id},
        )


def _outcomes(connection, node_id: str, attempt_id: str) -> list[tuple[str, str]]:
    """Each reported run of this attempt and its outcome, oldest first."""
    found = []
    for row in _events(connection, node_id, attempt_id, SETTLED, NO_RESULT):
        payload = json.loads(row["payload_json"])
        outcome = "no_result" if row["event"] == NO_RESULT else str(payload.get("outcome"))
        found.append((str(payload.get("run_id")), outcome))
    return found


def settle_step_from_script(
    connection, *, node_id: str, attempt_id: str, run_id: str, verdict: str | None,
    findings: str | None, version: str, cause: str | None = None, package_wide: bool = False,
) -> str:
    """Settle the step from one script run's report; ``accepted``, ``sent_back``, ``stale``,
    ``no_result`` or ``already``.

    A repeat of a reported run returns its first outcome and writes nothing; a
    different run for an attempt already settled is ``already``. Work whose
    version moved since the pending settle was recorded goes back to its author
    not started (``stale``), the attempt superseded. No verdict is ``no_result``:
    its cause is recorded, the pending settle cleared, and the step stays in review.
    """
    if verdict is not None and verdict not in review_settle.VERDICTS:
        raise service.PlanInputError("verdict must be 'pass', 'fail' or None")
    with service._write(connection):
        reported = _outcomes(connection, node_id, attempt_id)
        for reported_run, outcome in reported:
            if reported_run == run_id:
                return outcome
        if any(outcome in _FINAL for _run, outcome in reported):
            return "already"
        pending = _pending(connection, node_id, attempt_id)
        if pending is None:
            raise service.PlanError(f"no pending settle for attempt {attempt_id} on {node_id}")
        event: dict[str, object] = {"attempt_id": attempt_id, "run_id": run_id}
        if verdict is None:
            service._event(
                connection, node_id, NO_RESULT, actor_id=None, provenance=review_settle.PROVENANCE,
                payload={**event, "cause": cause, "package_wide": bool(package_wide)},
            )
            return "no_result"
        verified_by = f"settle-script:{run_id}"
        if version != pending.get("version"):
            review_settle.send_back(
                connection, node_id, findings=STALE, reason=STALE, verified_by=verified_by,
                correction=False, tell_author=True,
            )
            outcome = "stale"
            event["superseded"] = True
        else:
            outcome, _wake = review_settle.settle(
                connection, node_id, verdict=verdict, findings=findings or "", verified_by=verified_by,
                reviewer=pending.get("reviewer_thread_id"),
            )
        service._event(
            connection, node_id, SETTLED, actor_id=None, provenance=review_settle.PROVENANCE,
            payload={**event, "outcome": outcome},
        )
        return outcome
