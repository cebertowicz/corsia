"""Start and settle the workers ``start_worker`` rules queue (R19, R23, R24; §7.4).

The runner's claim alone decides who gets the next Claude Code slot: no worker exists
until a start is claimed with a slot free. Each pass runs under one lock, which every
spawn it makes runs inside, so no start is claimed twice:

1. a started start a restart cut short (no worker, or a worker with no turn, no queued
   input and no process) settles ``no_result`` and releases its identity;
2. with ``n`` slots free under the Claude Code limit, the ``n`` oldest queued starts are
   claimed, each checked when it would start: its rule turned off or gone, or its owner
   gone or paused, skips it (no replay; the identity is released) and takes no slot. At
   no free slot nothing is claimed or written, and the starts wait in the queue. A
   claimed start's inputs and brief are made (``automation.worker_inputs``) and it starts
   through ``delegate_task``'s own capped spawn with the ``review`` confinement on the
   owner's model. A Local only owner's start (the chat's own, or its project's cloud
   off) binds a ready local model instead, the owner's own when that is one; with none
   ready it is not claimed and waits ``queued``, held, keeping its place, and a catalog
   change runs a pass (:func:`poke_on_catalog_change`). Spawn records the worker on the
   start (``rule_start``) before its first turn can run. If another spawn took the slot first, the start goes back to the queue
   at its place, never refused for it. Any other refusal ends it ``refused`` and tells
   the owner through ``worker.no_result``;
3. each worker that was already started when the pass began and whose last turn ended settles
   once (``automation.worker_settle``); a worker made in this pass is observed on the next
   pass, so its start is visible before its completion.

Every way a start ends (refused, cut short, settled) goes through :func:`_settle`, which
reports it with what its row kept (its trace and workflow) and removes its inputs.

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
from events.rule_outcomes import NO_RESULT, REFUSED, SKIPPED
from infra.database import write_transaction
from infra.db_async import run_in_db

#: Starts claimed per pass at most; the rest wait for the next.
BATCH = 8
_RESTARTED = "a restart cut the start short"
HELD_LOCAL = "held: local only, no local model ready"
_SPAWN_FAILED = "refused: the start failed (delegation error)"
_FAILED = "refused: the start failed"
# "waited": the newest decision id queued while no slot was free. Process memory: it only
# feeds the settle's product event, and a restart forgets it.
_state: dict[str, Any] = {"lock": None, "task": None, "again": False, "open": False, "waited": 0}


class _HeldLocal(Exception):
    """A Local only owner's claimed start found no ready local model: it goes back, held."""


def _lock() -> asyncio.Lock:
    """The one lock of this event loop (a lock is bound to the loop that first waits on it)."""
    loop = asyncio.get_running_loop()
    if _state["lock"] is None or _state["lock"][0] is not loop:
        _state["lock"] = (loop, asyncio.Lock())
    return _state["lock"][1]


def poke(*, if_open: bool = False) -> None:
    """Run a pass soon (with ``if_open``, only while the last pass left a start open); one
    already running runs once more instead of a second starting. Best-effort: it never
    raises and never waits for the pass."""
    if if_open and not _state["open"]:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        task = _state["task"]
        if task is not None and not task.done():
            _state["again"] = True
            return
        _state["task"] = loop.create_task(_passes())
    except Exception:  # noqa: BLE001 - a turn end must never fail on the runner
        logger.exception("worker runner poke failed")


def poke_on_catalog_change() -> None:
    """A catalog change may have made a local model ready for a held start: run a pass
    while one is open, from the loop or a DB worker alike."""
    import infra.app_loop as app_loop

    async def _on_loop() -> None:
        poke(if_open=True)

    try:
        app_loop.submit(_on_loop())
    except app_loop.AppLoopUnavailable:
        pass  # no loop, no runner: the continuation sweep runs a pass after a start


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
    from subagents.cc_runtime_state import _LIVE

    async with _lock():
        for row in await run_in_db(_read, worker_starts.orphans):
            if row["worker_thread_id"] is None or _LIVE.get(row["worker_thread_id"]) is None:
                await _end(row, json.loads(row["run_input"] or "{}"), NO_RESULT, _RESTARTED)
        claimed = await run_in_db(_claim)
        claimed_ids = {row["id"] for row, _run_input in claimed}
        made = 0
        for row, run_input in claimed:
            made += await _start(row, run_input)
        for item in await run_in_db(_read, worker_settle.ready):
            if item.decision_id not in claimed_ids:
                await _settle(item)
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


def _free(conn: sqlite3.Connection) -> int:
    """Claude Code slots free now, by the count spawn's own limit check uses."""
    from infra.subagent_caps import get_max_concurrent_live
    from subagents.cc_idle_retirement import working_thread_ids

    return get_max_concurrent_live() - len(working_thread_ids(db=conn))


def _claim(db: sqlite3.Connection | None = None) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
    """Claim up to one queued start per free slot, oldest first. With no slot free nothing
    is written: the starts wait, and the newest of them is remembered as having waited.
    A held start keeps its place and takes no slot, so the read goes on past it."""
    from infra.queries import get_thread

    claimed: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    with write_transaction(db, scope="worker_starts_claim") as conn:
        free = _free(conn)
        if free <= 0:
            newest = conn.execute("SELECT MAX(id) FROM event_route_decisions").fetchone()[0]
            _state["waited"] = max(_state["waited"], newest or 0)
            return claimed
        ready: list[tuple[str, ...]] = []
        after, limit = 0, min(free, BATCH)
        while len(claimed) < limit and (rows := worker_starts.queued(conn, limit - len(claimed), after)):
            for row in rows:
                after = row["id"]
                skipped = _skip(conn, row)
                if skipped is not None:
                    worker_starts.end(conn, row["id"], SKIPPED, skipped)
                elif _local_model(conn, get_thread(row["thread_id"], db=conn), ready) is False:
                    worker_starts.hold(conn, row["id"], HELD_LOCAL)
                elif (run_input := worker_starts.claim(conn, row["id"])) is not None:
                    claimed.append((row, run_input))
    return claimed


def _local_model(conn: sqlite3.Connection, owner: Mapping[str, Any],
                 ready: list[tuple[str, ...]]) -> str | bool | None:
    """The ready local model a Local only owner's start binds (its own model when that is
    one), ``False`` when none is ready, ``None`` when the owner is not Local only. ``ready``
    caches the catalog's ready local models for one pass."""
    from infra.local_only import inherited_local_only, ready_local_selectors
    from llm.locality import locality_cause, project_cloud_models_off

    if locality_cause(thread_local_only=inherited_local_only((owner["id"],), db=conn),
                      cloud_off=project_cloud_models_off(owner["project_id"], db=conn)) is None:
        return None
    if not ready:
        ready.append(ready_local_selectors())
    models = ready[0]
    if owner["model"] and owner["model"] in models:
        return owner["model"]
    return models[0] if models else False


def _prepare(row: Mapping[str, Any], run_input: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the start reads from the database, or :class:`worker_inputs.StartRefused`,
    or :class:`_HeldLocal` when its Local only owner's last ready local model went away."""
    from infra.config import WORKSPACE
    from infra.database import get_db
    from infra.queries import get_thread
    from events.rule_actions import reachable_workflows
    from tools.workflow_task_fields import _workflow_version

    db = get_db()
    owner = dict(get_thread(row["thread_id"], db=db))
    project = owner.get("project_id")
    local = _local_model(db, owner, [])
    if local is False:
        raise _HeldLocal
    # A skill whose declared result fails validation is not included, so one found here is valid.
    skill = reachable_workflows(db, owner["id"], project, WORKSPACE).get(row["workflow_id"])
    schema = ((skill.corsia or {}).get("workflow") or {}).get("result") if skill else None
    if skill is None or schema is None:
        raise inputs.StartRefused(f"refused: {row['workflow_id']} is no longer a workflow you can "
                                  "read that declares a valid result")
    facts = (run_input.get("event") or {}).get("facts") or {}
    root = inputs.owner_root(owner["id"], project, db)
    return {"owner": owner, "model": local or owner.get("model"),
            "skill": skill, "schema": schema, "root": root, "facts": facts,
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
    """Start one claimed request's worker; 1 if it was made."""
    from infra.subagent_caps import LiveSpawnCapExceeded
    from subagents.cc_invocation import CONFINEMENT_REVIEW
    from tools.delegate_task import _spawn

    waited = row["id"] <= _state["waited"]
    try:
        found = await run_in_db(_prepare, row, run_input)
        read_paths, task = await asyncio.to_thread(_write, found, row["id"], run_input)
        check = json.dumps({"workflow": found["workflow"], "schema": found["schema"],
                            "trace": run_input.get("trace") or [], "waited": waited,
                            "item_version": found["facts"].get(inputs.HEAD_SHA_FACT)})

        async def record(worker: str) -> None:
            await run_in_db(_made, row["id"], worker, check)

        started = await _spawn(row["thread_id"], found["owner"], task, {
            "model": found["model"], "confinement": CONFINEMENT_REVIEW,
            "read_paths": read_paths, "title": found["skill"].name,
        }, rule_start=record)
    except inputs.StartRefused as exc:
        return await _refuse(row, run_input, str(exc), waited)
    except LiveSpawnCapExceeded:
        # Another spawn took the slot after the claim: back to the queue at its place.
        _state["waited"] = max(_state["waited"], row["id"])
        await run_in_db(_requeue, row["thread_id"], row["id"])
        return 0
    except _HeldLocal:
        await run_in_db(_requeue, row["thread_id"], row["id"], HELD_LOCAL)
        return 0
    except Exception:  # noqa: BLE001 - a start that fails is told to its owner, not lost
        logger.exception("worker start {} failed", row["id"])  # the exception stays in the log
        return await _refuse(row, run_input, _FAILED, waited)
    if isinstance(started, str):
        cause = started.removeprefix("Error: ")
        if not cause.startswith(worker_settle.REFUSAL_PREFIX):
            logger.warning("worker start {} was refused: {}", row["id"], started)
            cause = _SPAWN_FAILED
        return await _refuse(row, run_input, cause, waited)
    return 1


def _made(decision_id: int, worker: str, check: str) -> None:
    with write_transaction(None, scope="worker_starts_made") as conn:
        worker_starts.made(conn, decision_id, worker, check)


def _requeue(owner: str, decision_id: int, held: str | None = None) -> None:
    with write_transaction(None, scope="worker_starts_requeue") as conn:
        if worker_starts.requeue(conn, decision_id) and held:
            worker_starts.hold(conn, decision_id, held)
    _remove_inputs(owner, decision_id)


async def _refuse(row: Mapping[str, Any], run_input: Mapping[str, Any], reason: str,
                  waited: bool) -> int:
    await _end(row, {**run_input, "waited": waited}, REFUSED, reason)
    return 0


async def _end(row: Mapping[str, Any], kept: Mapping[str, Any], outcome: str, reason: str) -> None:
    """End a start no worker result settles, reported with what its row ``kept``: the
    claimed input (its trace) or, once its worker was made, its check."""
    check = {"workflow": row["workflow_id"], "waited": row["id"] <= _state["waited"], **kept}
    await _settle(worker_settle.settlement(row, check, outcome, {"reason": reason}))


async def _settle(item: worker_settle.Settle) -> None:
    """Settle one start and remove its inputs: the one way every start ends."""
    await worker_settle.settle(item)
    await run_in_db(_remove_inputs, item.owner, item.decision_id)


def _remove_inputs(owner: str, decision_id: int) -> None:
    from infra.database import get_db
    from infra.queries import get_thread

    db = get_db()
    thread = get_thread(owner, db=db)
    if thread is not None:
        inputs.remove_start_dir(inputs.owner_root(owner, thread["project_id"], db), decision_id)
