"""Start, release and settle the workers ``start_worker`` rules queue (R19, R23, R24; §7.4).

Each pass runs under one lock, so a freed Claude Code slot goes to exactly one held
start, oldest first, and no start is claimed twice:

1. a claimed start with no worker is one a restart cut short: ``no_result``;
2. each queued start is checked when it would start: its rule turned off or gone, or its
   owner gone or paused, skips it (no replay; the identity is released). Otherwise it is
   claimed, its inputs and brief are made (``automation.worker_inputs``), and it starts
   through ``delegate_task``'s own spawn with the ``review`` confinement on the owner's
   model; at a full cap the worker is made and its first turn held. Spawn records the
   worker on the start (``rule_start``) before its first turn can run. A refusal ends it
   ``refused`` and tells the owner through ``worker.no_result``;
3. each held start is offered the idle claim, which lifts its hold only while a slot is
   left after every older one (``infra.worker_capacity.lift``); one whose hold is gone is
   ``started``;
4. each started worker whose last turn ended settles once (``automation.worker_settle``).

A pass runs on the continuation sweep when any start is open, which also covers a
restart, and on every turn end while the last pass left one open (a slot may have
freed, a worker may be done); with none open a turn end costs nothing.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from loguru import logger

from automation import worker_inputs as inputs
from automation import worker_settle
from events import rule_runs, worker_starts
from events.rule_outcomes import HELD, NO_RESULT, REFUSED, SKIPPED
from infra.database import write_transaction
from infra.db_async import run_in_db

#: Queued starts claimed per pass; the rest wait for the next.
BATCH = 8
_RESTARTED = "the backend restarted before the worker was made"
_LOCAL_ONLY = "refused: local only, no confined local worker yet"
_state: dict[str, Any] = {"lock": None, "task": None, "again": False, "open": False}


def _lock() -> asyncio.Lock:
    """The one lock of this event loop (a lock is bound to the loop that first waits on it)."""
    loop = asyncio.get_running_loop()
    if _state["lock"] is None or _state["lock"][0] is not loop:
        _state["lock"] = (loop, asyncio.Lock())
    return _state["lock"][1]


def poke(*, if_open: bool = False) -> None:
    """Run a pass soon (with ``if_open``, only while the last pass left a start open); one
    already running runs once more instead of a second starting."""
    if if_open and not _state["open"]:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _state["task"]
    if task is not None and not task.done():
        _state["again"] = True
        return
    _state["task"] = loop.create_task(_passes())


async def _passes() -> None:
    while True:
        _state["again"] = False
        try:
            await drive()
        except Exception:  # noqa: BLE001 - a pass must never take the turn end or sweep down
            logger.exception("worker runner pass failed")
        if not _state["again"]:
            return


async def drive() -> int:
    """One pass; how many workers it made."""
    async with _lock():
        for row in await run_in_db(_read, worker_starts.orphans):
            await _end(row, {}, NO_RESULT, _RESTARTED)
        made = 0
        for row, run_input in await run_in_db(_claim):
            made += await _start(row, run_input)
        for row in await run_in_db(_read, worker_starts.with_workers, HELD):
            await _release(row)
        for item in await run_in_db(_read, worker_settle.ready):
            await worker_settle.settle(item)
            await run_in_db(_remove_inputs, item.owner, item.decision_id)
        _state["open"] = await run_in_db(worker_starts.any_open)
        return made


def _read(fn, *args):
    from infra.database import get_db

    return fn(get_db(), *args)


def _skip(conn: sqlite3.Connection, row: Mapping[str, Any]) -> str | None:
    """Why a queued start can't begin now, checked at the time of its effect."""
    from infra.queries import get_thread, thread_is_available
    from plans.public import thread_paused_since

    if not row["enabled"] or row["workflow_id"] is None:
        return rule_runs.TURNED_OFF
    if not thread_is_available(get_thread(row["thread_id"], db=conn)):
        return rule_runs.OWNER_GONE
    if thread_paused_since(conn, row["thread_id"]) is not None:
        return rule_runs.OWNER_PAUSED
    return None


def _claim(db: sqlite3.Connection | None = None) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
    claimed = []
    with write_transaction(db, scope="worker_starts_claim") as conn:
        for row in worker_starts.queued(conn, BATCH):
            skipped = _skip(conn, row)
            if skipped is not None:
                worker_starts.end(conn, row["id"], SKIPPED, skipped)
                continue
            run_input = worker_starts.claim(conn, row["id"])
            if run_input is not None:
                claimed.append((row, run_input))
    return claimed


def _prepare(row: Mapping[str, Any], run_input: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the start reads from the database, or :class:`worker_inputs.StartRefused`.
    Local only refuses here, by the existing locality policy: no confined local worker exists."""
    from infra.config import WORKSPACE
    from infra.database import get_db
    from infra.local_only import inherited_local_only
    from infra.queries import get_thread
    from llm.locality import locality_cause, project_cloud_models_off
    from events.rule_actions import reachable_workflows
    from tools.workflow_task_fields import _workflow_version

    db = get_db()
    owner = dict(get_thread(row["thread_id"], db=db))
    project = owner.get("project_id")
    if locality_cause(thread_local_only=inherited_local_only((owner["id"],), db=db),
                      cloud_off=project_cloud_models_off(project, db=db)):
        raise inputs.StartRefused(_LOCAL_ONLY)
    # A skill whose declared result fails validation is not included, so one found here is valid.
    skill = reachable_workflows(db, owner["id"], project, WORKSPACE).get(row["workflow_id"])
    schema = ((skill.corsia or {}).get("workflow") or {}).get("result") if skill else None
    if skill is None or schema is None:
        raise inputs.StartRefused(f"refused: {row['workflow_id']} is no longer a workflow you can "
                                  "read that declares a valid result")
    facts = (run_input.get("event") or {}).get("facts") or {}
    root = inputs.owner_root(owner["id"], project, db)
    return {"owner": owner, "skill": skill, "schema": schema, "root": root, "facts": facts,
            "workflow": f"{skill.name}@{_workflow_version(skill) or 'unversioned'}",
            "staging": inputs.staging_dir(root, facts.get(inputs.STAGING_FACT)),
            "head": inputs.head_source(project, facts, db)}


def _write(found: Mapping[str, Any], decision_id: int, run_input: Mapping[str, Any]) -> tuple[list[str], str]:
    """The worker's read paths and brief; writes its head tree (file work, off the DB pool)."""
    dest = inputs.start_dir(found["root"], decision_id)
    dest.mkdir(parents=True, exist_ok=True)
    if found["head"] is not None:
        inputs.write_head(found["head"], dest)
    read_paths = [path for path in (found["staging"], str(dest)) if path]
    task = inputs.brief(found["workflow"], inputs.skill_body(found["skill"].body_path),
                        found["schema"], read_paths, run_input.get("event") or {})
    return read_paths, task


async def _start(row: Mapping[str, Any], run_input: dict[str, Any]) -> int:
    """Start one claimed request's worker; 1 if it was made (started or held)."""
    from subagents.cc_invocation import CONFINEMENT_REVIEW
    from tools.delegate_task import _spawn

    try:
        found = await run_in_db(_prepare, row, run_input)
        read_paths, task = await asyncio.to_thread(_write, found, row["id"], run_input)
        check = json.dumps({"workflow": found["workflow"], "schema": found["schema"],
                            "trace": run_input.get("trace") or [],
                            "item_version": found["facts"].get(inputs.HEAD_SHA_FACT)})

        async def record(worker: str, held: bool) -> None:
            await run_in_db(_made, row["id"], worker, held, check)

        started = await _spawn(row["thread_id"], found["owner"], task, {
            "model": found["owner"].get("model"), "confinement": CONFINEMENT_REVIEW,
            "read_paths": read_paths, "title": f"{found['skill'].name} worker",
        }, rule_start=record)
    except inputs.StartRefused as exc:
        return await _refuse(row, str(exc))
    except Exception as exc:  # noqa: BLE001 - a start that fails is told to its owner, not lost
        logger.exception("worker start {} failed", row["id"])
        return await _refuse(row, f"refused: the start failed: {exc}")
    if isinstance(started, str):
        cause = started.removeprefix("Error: ")
        return await _refuse(row, cause if cause.startswith("refused: ") else f"refused: {cause}")
    return 1


def _made(decision_id: int, worker: str, held: bool, check: str) -> None:
    with write_transaction(None, scope="worker_starts_made") as conn:
        worker_starts.made(conn, decision_id, worker, held=held, check=check)


def _held(conn: sqlite3.Connection, worker: str) -> bool:
    """Whether the worker's first turn waits for a working slot."""
    from infra.worker_capacity import WORKER_CAPACITY

    return conn.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND write_outcome='proved_no_write' AND delivery_error=? LIMIT 1", (worker, WORKER_CAPACITY),
    ).fetchone() is not None


async def _refuse(row: Mapping[str, Any], reason: str) -> int:
    await _end(row, {}, REFUSED, reason)
    await run_in_db(_remove_inputs, row["thread_id"], row["id"])
    return 0


async def _end(row: Mapping[str, Any], check: Mapping[str, Any], outcome: str, reason: str) -> None:
    await worker_settle.settle(worker_settle.settlement(row, check, outcome, {"reason": reason}))


async def _release(row: Mapping[str, Any]) -> None:
    """Offer a held worker the idle claim; once its hold is gone, its start is started."""
    from infra.thread_execution import drive_idle_thread

    worker = row["worker_thread_id"]
    if await run_in_db(_read, _held, worker):
        await drive_idle_thread(worker)
    await run_in_db(_running, row["id"], worker)


def _running(decision_id: int, worker: str) -> None:
    with write_transaction(None, scope="worker_starts_running") as conn:
        if not _held(conn, worker):
            worker_starts.running(conn, decision_id)


def _remove_inputs(owner: str, decision_id: int) -> None:
    from infra.database import get_db
    from infra.queries import get_thread

    db = get_db()
    thread = get_thread(owner, db=db)
    if thread is not None:
        inputs.remove_start_dir(inputs.owner_root(owner, thread["project_id"], db), decision_id)
