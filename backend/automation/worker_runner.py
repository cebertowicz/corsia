"""Start and settle the workers ``start_worker`` rules queue (R19, R23, R24; §7.4).

The runner's claim alone decides who gets the next worker slot: no worker exists
until a start is claimed with a slot free. Each pass runs under one lock, which every
spawn it makes runs inside, so no start is claimed twice:

1. a started start a restart cut short (no worker, or a worker with no turn, no queued
   input and no process) settles ``no_result`` and releases its identity;
2. with ``n`` slots free under the worker limit (Claude Code workers and rule workers),
   the ``n`` oldest queued starts are claimed, each checked when it would start: its rule turned off or gone, or its owner
   gone or paused, skips it (no replay; the identity is released) and takes no slot. At
   no free slot nothing is claimed or written, and the starts wait in the queue. A
   claimed start's inputs and brief are made (``automation.worker_inputs``) and it starts
   through ``delegate_task`` with the ``untrusted`` confinement on the
   owner's model. Local only owners run on their own local model. A model that cannot
   run now is refused and reported through ``worker.no_result``. Spawn records the worker
   on the start (``rule_start``) before its first turn can run. If another spawn took the
   slot first, the start goes back to the queue
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

#: Queued starts read per pass; the rest wait for the next.
BATCH = 8
_RESTARTED = "a restart cut the start short"
_SPAWN_FAILED = "refused: the start failed (delegation error)"
_FAILED = "refused: the start failed"
# "waited": the newest decision id queued while no slot was free. Process memory: it only
# feeds the settle's product event, and a restart forgets it.
_state: dict[str, Any] = {"lock": None, "task": None, "again": False, "open": False, "waited": 0}


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
    """Slots free under the shared worker limit."""
    from infra.subagent_caps import get_max_concurrent_live
    from subagents.cc_idle_retirement import working_thread_ids

    return get_max_concurrent_live() - len(set(working_thread_ids(db=conn)) | worker_starts.started_workers(conn))


def _claim(db: sqlite3.Connection | None = None) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
    """Claim up to one queued start per free slot, oldest first. With no slot free nothing
    is written: the starts wait, and the newest of them is remembered as having waited."""
    from events.connectors import shipped_result_hook

    claimed: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    cleanup: list[tuple[str, int, str | None]] = []
    with write_transaction(db, scope="worker_starts_claim") as conn:
        free = _free(conn)
        if free <= 0:
            newest = conn.execute("SELECT MAX(id) FROM event_route_decisions").fetchone()[0]
            _state["waited"] = max(_state["waited"], newest or 0)
            return claimed
        for row in worker_starts.queued(conn, BATCH):
            if len(claimed) == free:
                break
            skipped = _skip(conn, row)
            if skipped is not None:
                if shipped_result_hook(row["origin"], row["workflow_id"]):
                    data = json.loads(row["run_input"] or "{}")
                    facts = (data.get("event") or {}).get("facts") or {}
                    cleanup.append((row["thread_id"], row["id"], facts.get(inputs.STAGING_FACT)))
                worker_starts.end(conn, row["id"], SKIPPED, skipped)
                continue
            run_input = worker_starts.claim(conn, row["id"])
            if run_input is not None:
                claimed.append((row, run_input))
    for owner, decision, staging in cleanup:
        _remove_inputs(owner, decision, staging)
    return claimed


def _prepare(row: Mapping[str, Any], run_input: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the start reads from the database, or :class:`worker_inputs.StartRefused`."""
    from infra.config import WORKSPACE
    from infra.database import get_db
    from infra.local_only import inherited_local_only
    from infra.queries import get_thread
    from events.rule_actions import reachable_workflows
    from llm.account_catalog import effective_status
    from llm.delegation_model import dispatch_scope
    from llm.locality import locality_refusal
    from llm.model_status import scoped_status_rows
    from llm.resolution import catalog_row
    from tools.workflow_task_fields import _workflow_version

    db = get_db()
    owner = dict(get_thread(row["thread_id"], db=db))
    project = owner.get("project_id")
    model = owner.get("model")
    if not model:
        raise inputs.StartRefused("refused: this chat has no model. Choose a model for this chat.")
    model_row = catalog_row(model, db=db)
    rows = scoped_status_rows(**dispatch_scope({}, project))
    status_row = next((item for item in rows if model in (item.get("id"), item.get("litellm_model"))), None)
    name = (status_row or {}).get("display_name") or (model_row or {}).get("display_name") or model
    cause = locality_refusal(model_row, thread_local_only=inherited_local_only((owner["id"],), db=db),
                             project_id=project, db=db) if model_row else None
    if cause == "thread_local_only":
        raise inputs.StartRefused(f"refused: this chat is set to Local only and your model {name} is a "
                                  "cloud model. Choose a local model for this chat, or turn off Local only.")
    if cause == "project_cloud_off":
        raise inputs.StartRefused(f"refused: this project has cloud models off and your model {name} is a "
                                  "cloud model. Choose a local model, or allow cloud models for this project.")
    if status_row is None:
        raise inputs.StartRefused("refused: this model can't be found. Choose another model for this chat.")
    if effective_status(status_row)[0] != "ready":
        if status_row.get("is_local"):
            raise inputs.StartRefused(f"refused: your model {name} is not loaded. "
                                      "Load it, or choose a local model that is loaded.")
        raise inputs.StartRefused(f"refused: your model {name} can't run now. "
                                  "Choose a model that is ready for this chat.")
    # A skill whose declared result fails validation is not included, so one found here is valid.
    from events.connectors import shipped_workflow

    skill = shipped_workflow(dict(row).get("origin"), row["workflow_id"], project)
    if skill is None:
        skill = reachable_workflows(db, owner["id"], project, WORKSPACE).get(row["workflow_id"])
    schema = ((skill.corsia or {}).get("workflow") or {}).get("result") if skill else None
    if skill is None or schema is None:
        raise inputs.StartRefused(f"refused: {row['workflow_id']} is no longer a workflow you can "
                                  "read that declares a valid result")
    facts = (run_input.get("event") or {}).get("facts") or {}
    root = inputs.owner_root(owner["id"], project, db)
    from llm.resolution import resolve_execution_target

    return {"owner": owner, "model": owner.get("model"),
            "harness": resolve_execution_target(model, db=db).harness,
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
    body = inputs.skill_body(found["skill"].body_path)
    if found.get("result_instructions"):
        body += "\n\n" + found["result_instructions"]
    task = inputs.brief(found["workflow"], body,
                        found["schema"], read_paths, run_input.get("event") or {})
    return read_paths, task


async def _start(row: Mapping[str, Any], run_input: dict[str, Any]) -> int:
    """Start one claimed request's worker; 1 if it was made."""
    from infra.subagent_caps import LiveSpawnCapExceeded
    from subagents.cc_invocation import CONFINEMENT_UNTRUSTED
    from tools.delegate_task import _spawn

    waited = row["id"] <= _state["waited"]
    try:
        found = await run_in_db(_prepare, row, run_input)
        if found["harness"] == "codex":
            from subagents.cc_confinement import ConfinementRefused
            from subagents.codex_confinement import verify_version

            try:
                await verify_version()
            except ConfinementRefused as exc:
                raise inputs.StartRefused(str(exc)) from exc
        from events.connectors import shipped_result_hook

        origin = dict(row).get("origin")
        hook = shipped_result_hook(origin, row["workflow_id"])
        if hook:
            found["result_instructions"] = hook[2]()
            found["workflow"] = f"{row['workflow_id']}@{hook[3]()}"
        read_paths, task = await asyncio.to_thread(_write, found, row["id"], run_input)
        captured = None
        if hook:
            try:
                captured = await asyncio.to_thread(hook[0], found["staging"])
            except Exception:  # A missing floor must invalidate, never clear, the result.
                logger.exception("worker start {} could not capture its result record", row["id"])
        check = json.dumps({"workflow": found["workflow"], "schema": found["schema"],
                            "trace": run_input.get("trace") or [], "waited": waited,
                            "item_version": found["facts"].get("item_version") or found["facts"].get(inputs.HEAD_SHA_FACT),
                            "hook_origin": origin if hook else None,
                            "hook_name": row["workflow_id"] if hook else None,
                            "hook_record": captured,
                            "staging_path": found["facts"].get(inputs.STAGING_FACT) if hook else None,
                            "event_facts": found["facts"] if hook else None})

        async def record(worker: str) -> None:
            await run_in_db(_made, row["id"], worker, check)

        started = await _spawn(row["thread_id"], found["owner"], task, {
            "model": found["model"], "confinement": CONFINEMENT_UNTRUSTED,
            "read_paths": read_paths, "title": found["skill"].name,
        }, rule_start=record)
    except inputs.StartRefused as exc:
        return await _refuse(row, run_input, str(exc), waited)
    except LiveSpawnCapExceeded:
        # Another spawn took the slot after the claim: back to the queue at its place.
        _state["waited"] = max(_state["waited"], row["id"])
        await run_in_db(_requeue, row["thread_id"], row["id"])
        return 0
    except Exception:  # noqa: BLE001 - a start that fails is told to its owner, not lost
        logger.exception("worker start {} failed", row["id"])  # the exception stays in the log
        return await _refuse(row, run_input, _FAILED, waited)
    if isinstance(started, str):
        logger.warning("worker start {} was refused: {}", row["id"], started)
        return await _refuse(row, run_input, _SPAWN_FAILED, waited)
    return 1


def _made(decision_id: int, worker: str, check: str) -> None:
    with write_transaction(None, scope="worker_starts_made") as conn:
        worker_starts.made(conn, decision_id, worker, check)


def _requeue(owner: str, decision_id: int) -> None:
    with write_transaction(None, scope="worker_starts_requeue") as conn:
        worker_starts.requeue(conn, decision_id)
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
    await run_in_db(_remove_inputs, item.owner, item.decision_id, item.staging_path)


def _remove_inputs(owner: str, decision_id: int, staging_path: str | None = None) -> None:
    from infra.database import get_db
    from infra.queries import get_thread

    db = get_db()
    thread = get_thread(owner, db=db)
    if thread is not None:
        root = inputs.owner_root(owner, thread["project_id"], db)
        inputs.remove_start_dir(root, decision_id)
        inputs.remove_staging_dir(root, staging_path)
