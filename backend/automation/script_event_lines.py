"""Sending a script run's event lines to the intake before the run settles.

The runner builds each line's event id from the run and the line's position, so a line
sent twice is answered once. That is what lets a line the intake could not take be sent
again here, and why a refused line's owner is told to run the script again, not to resend it.
"""

from __future__ import annotations

import asyncio
import dataclasses

from loguru import logger

from automation.schedules import Schedule
from automation.script_readings import RunReport, Signal
from infra.database import get_db
from infra.db_async import run_in_db_unless_owned

#: The runner builds a line's event id from the run, so the owner can't resend it: a new run sends every line again.
_RERUN_FIX = "Run the script again to send it; its other event lines are sent again too."
#: A run a rule started has no next run and its owner cannot start it with the same event.
_RULE_RUN_FIX = "A run a rule started is not run again. If its work still matters, do it for its item yourself."
#: The pause before each further try of an event line the intake could not take. One more try than pauses;
#: each try can itself wait out the write queue, so a line is given up after a few minutes, never held.
_ADMIT_PAUSE_SECONDS: tuple[float, ...] = (5, 15, 30)


async def _admit_line(schedule_id: str, index: int, tries: int, **event) -> tuple[str, str] | None:
    """Send one event line; the refusal's code and text if it was not taken.

    A line the intake could not take (a write queue that ran out, a database it could not open)
    is sent again under the same event id, which the intake answers once however many tries land.
    """
    from events.intake import admit_event
    from events.intake_errors import failure_code

    for attempt in range(1, tries + 1):
        try:
            result = await admit_event(**event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - run_claimed never raises; the run settles as an error
            code = failure_code(exc)
            if code == "intake_unreachable" and attempt < tries:
                logger.warning("script schedule {} event line {} could not reach the intake, try {} of {}: {}",
                               schedule_id, index, attempt, tries, exc)
                await asyncio.sleep(_ADMIT_PAUSE_SECONDS[attempt - 1])
                continue
            logger.exception("script schedule {} event line {} failed inside the intake", schedule_id, index)
            return code, ("the event intake failed unexpectedly" if code == "internal_error"
                          else "event intake is unavailable")
        return (result.refusal.code, result.refusal.text) if result.refusal is not None else None
    return None  # unreachable: the last try returns


async def admit_events(schedule: Schedule, report: RunReport, trace: tuple[str, ...] = (),
                       rule_decision_id: int | None = None) -> RunReport:
    """Route parsed event lines before synchronous settlement; one refusal wakes the owner once.

    ``trace`` is the rule chain of a rule-started run; each event it sends carries it.
    """
    if not report.events:
        return report
    from automation.script_measures import run_stamps
    from events.intake_errors import format_event_error

    stamps = run_stamps(schedule, report)
    producer = f"script:{schedule.id}"
    refusals: dict[str, str] = {}  # each distinct code, with the first text it came with
    for index, event in enumerate(report.events):
        try:
            # Once one line has used every try, the intake is down: later lines get one try each,
            # so an outage never holds a run slot for every line's full wait.
            tries = 1 if "intake_unreachable" in refusals else len(_ADMIT_PAUSE_SECONDS) + 1
            refused = await _admit_line(
                schedule.id, index, tries,
                event_type=event["type"], facts=event["facts"], origin="emitter",
                producer=producer, event_id=f"{producer}:{report.run_id}:{index}", stamps=stamps,
                **({"trace": trace} if trace else {}),
                **({"rule_decision_id": rule_decision_id} if rule_decision_id is not None else {}),
            )
            if refused is not None:
                refusals.setdefault(*refused)
        finally:
            if (schedule.payload or {}).get("event_type") == event["type"] and event["facts"].get("staging_path"):
                from automation.worker_inputs import remove_unclaimed_staging

                await run_in_db_unless_owned(
                    None, lambda: remove_unclaimed_staging(
                        schedule.thread_id, schedule.scope_id, f"{producer}:{report.run_id}:{index}",
                        event["facts"]["staging_path"], get_db()),
                )
    if not refusals:
        return report
    unreachable_fix = _RERUN_FIX if rule_decision_id is None else _RULE_RUN_FIX
    shown = [
        format_event_error(code, refusals[code], unreachable_fix if code == "intake_unreachable" else None)
        for code in sorted(refusals)[:2]
    ]
    more = f" and {len(refusals) - 2} more" if len(refusals) > 2 else ""
    message = f"event line refused: {' '.join(shown)}{more}"
    return dataclasses.replace(
        report, status="error", error=message,
        signals=[*report.signals, Signal(
            "error", None, f'Script "{(schedule.script_path or "script").rsplit("/", 1)[-1]}": {message}',
            {"message": message},
        )],
    )
