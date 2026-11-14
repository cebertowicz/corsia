"""Claim the script runs ``run_script`` rules queued, for the script runner's sweep.

A queued run is checked when it would start, the time of its effect: a run whose
owner is paused, whose rule was turned off, or whose script schedule or owner is
gone is skipped with the reason, since it never started. None of these wakes
anyone; ``explain`` and ``list`` show them. A rule run takes its script's own
in-flight claim, the one a scheduled run takes, so at most one run of a script
is in flight; a run whose script is busy stays queued for a later sweep. The
queue itself is ``events.rule_runs``. The repository a run may read GitHub for is
taken from that stored input's event here, before the script sees any of it.

A run that did start and that the backend lost before it settled is a failed run, so
its rule's owner is woken with the runs it lost; the wake commits with their rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from automation import script_schedules as store
from automation.script_github_channel import bound_repository
from automation.script_measures import WORKSPACE_SCOPE
from automation.script_readings import wake_owner
from automation.schedules import Schedule, get
from events import rule_runs
from infra.database import write_transaction
from infra.queries import get_thread, thread_is_available

#: The key a rule run holds in the runner's in-flight map, beside the schedule ids.
KEY_PREFIX = "rule:"
#: The wake for runs the backend lost; a scheduled run's ``interrupted`` status wakes nobody.
INTERRUPTED_EVENT = "script.interrupted"
_INTERRUPTED_NEXT = (
    "Nothing starts these runs again. This wake does not mean your script or rule is at fault. "
    "If a run's work still matters, do it for its item yourself. "
    "event_rules explain with a run's event_id shows its record."
)


@dataclass(frozen=True)
class RuleRun:
    """One claimed rule run: its decision row, the event for stdin, the rule chain, and
    the repository its stored connector, platform or emitter event names, the only one its GitHub
    channel reads."""

    decision_id: int
    rule_id: str
    stdin: bytes
    trace: tuple[str, ...]
    github_repo: str | None = None

    @property
    def key(self) -> str:
        return f"{KEY_PREFIX}{self.decision_id}"


def execution_input(schedule: Schedule, run: RuleRun) -> tuple[bytes, str | None]:
    """Bind the claimed repository, off-lock, to the repositories the run's project
    registers now; a workspace-scoped script has no project and binds none.

    The run's project is the script's ``scope_id``, set from its owner's thread when the
    script was made: the one source the sandbox's repository policies, the staging root,
    staging cleanup and measures also read. Shipped event producers also receive their
    output declaration and current staging root in stdin, rather than a copied
    configuration file.
    """
    from automation.worker_inputs import owner_root
    from events.connectors import get_manifest
    from infra.database import get_db

    repository = None
    if run.github_repo is not None and schedule.scope_id != WORKSPACE_SCOPE:
        resolve = get_manifest("github").project_repositories
        repositories = resolve(schedule.scope_id) if resolve is not None else None
        if repositories is not None:
            repository = next((repo for repo in repositories if repo.casefold() == run.github_repo.casefold()), None)
    event_type = (schedule.payload or {}).get("event_type")
    if event_type is None:
        return run.stdin, repository
    event = json.loads(run.stdin)
    root = owner_root(schedule.thread_id, schedule.scope_id, get_db())
    event["context"] = {"project_id": schedule.scope_id, "staging_root": str(root / "staging"),
                        "event_type": event_type, "github_repo": repository}
    return json.dumps(event, ensure_ascii=False).encode(), repository


def live_decisions(keys: set[str]) -> set[int]:
    """The decision ids of the rule runs this process holds."""
    return {int(key[len(KEY_PREFIX):]) for key in keys if key.startswith(KEY_PREFIX)}


def _refusal(connection, row) -> tuple[Schedule | None, str | None]:
    """The run's schedule, or why it can't start now."""
    if not row["enabled"]:
        return None, rule_runs.TURNED_OFF
    try:
        schedule = get(row["script_id"], db=connection)
    except KeyError:
        return None, "its script schedule no longer exists"
    if schedule.state != "active":
        return None, f"its script schedule is {schedule.state}"
    if not thread_is_available(get_thread(row["thread_id"], db=connection)):
        return None, rule_runs.OWNER_GONE
    return schedule, None


def _report_interrupted(connection, ended: Iterable[Mapping[str, Any]]) -> None:
    """Wake each owner once with the runs of theirs the backend lost, in the caller's write.

    The wake's id comes from the rows it names, never the script's one-a-day failure key,
    so a day's earlier failure can't turn it into a note. It is sent the way every script
    failure is (``script_readings.wake_owner``): an owner who is gone is parked, and the
    person's line names no rule of theirs.
    """
    from events.delivery import start_idle_after_commit

    by_owner: dict[str, list[Mapping[str, Any]]] = {}
    for row in ended:
        by_owner.setdefault(row["thread_id"], []).append(row)
    for owner, rows in by_owner.items():
        runs = [{k: v for k, v in {
            "rule_id": row["rule_id"], "event_id": row["event_id"],
            "event_type": f"{row['connector']}.{row['event_type']}", "item": row["artifact_external_id"],
        }.items() if v is not None} for row in rows]
        # A row left running with no task says the run stopped, never why: a restart is one
        # way there, a runner task that died in this process is another. Name no cause. The
        # loss is CORSIA's either way: a script's own failure settles as a report.
        summary = ("CORSIA lost track of a script run your event rule started and has no result for it. "
                   "It is not replayed."
                   if len(runs) == 1 else
                   f"CORSIA lost track of {len(runs)} script runs your event rules started and has no result "
                   "for them. They are not replayed.")
        chip = ("Script run stopped before it finished" if len(runs) == 1
                else f"{len(runs)} script runs stopped before they finished")
        details = {"runs": runs, "next": _INTERRUPTED_NEXT}
        key = ",".join(str(row["id"]) for row in rows)
        if not wake_owner(
            connection, owner, INTERRUPTED_EVENT, summary, details, chip=chip, park_key=key,
            message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia-rule-runs-interrupted:{owner}:{key}")),
        ):
            logger.warning("rule runs {} of owner {} were interrupted; the owner is unavailable", key, owner)
            continue
        start_idle_after_commit(connection, owner)


def claim(now: str, capacity: int, live_keys: set[str], db) -> list[tuple[Schedule, RuleRun]]:
    """End interrupted rule runs and tell their owners, then claim up to ``capacity`` queued
    ones, oldest first."""
    from plans.public import thread_paused_since

    claimed: list[tuple[Schedule, RuleRun]] = []
    with write_transaction(db, scope="script_rule_runs_claim") as connection:
        _report_interrupted(connection, rule_runs.interrupt(connection, live_decisions(live_keys)))
        for row in rule_runs.queued(connection, capacity):
            schedule, refused = _refusal(connection, row)
            if refused is None and thread_paused_since(connection, row["thread_id"]) is not None:
                refused = rule_runs.OWNER_PAUSED
            if schedule is None or refused is not None:
                rule_runs.finish(connection, row["id"], skipped=refused)
                continue
            if not store.claim_for_rule(schedule.id, now, db=connection):
                continue  # an earlier row of this sweep took the script: this one waits its turn
            # Still queued under this write lock; were it not, the next sweep frees the claim.
            run_input = rule_runs.start(connection, row["id"])
            if run_input is not None:
                event = run_input.get("event", {})
                claimed.append((schedule, RuleRun(
                    row["id"], row["rule_id"],
                    json.dumps(event, ensure_ascii=False).encode(),
                    tuple(run_input.get("trace", ())),
                    bound_repository(event),
                )))
    return claimed
