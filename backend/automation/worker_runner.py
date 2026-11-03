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
   pass, so its start is visible before its completion;
4. a started worker still running past its workflow's time limit is stopped and settles
   ``no_result`` the same way (see below).

A workflow's ``worker`` block (``core.workflow_worker``) sets what its worker runs with,
read at start with the rule's ``worker_profile``. The worker still starts on the owner's
model, never a model the runner picks. ``effort`` must be one the model lists. ``max_turns``
is passed to Claude Code only (``--max-turns``); another harness has no per-run turn cap, and
the start's record says so. ``min_context_tokens`` is read against the model's effective
window (``llm.context_window``), never the provider's; a Claude Code model that falls short but
whose provider window meets it runs as its larger-context variant (``[1m]``), and any other
shortfall refuses the start, never a silent fallback to the smaller window. What was applied
is kept on the start's row (``settings``), with the time it started: that is how the limit is
checked, on the passes above, so an overrun is stopped on the next pass after its limit, never
by a timer of its own.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
        settled = set()
        for item in await run_in_db(_read, worker_settle.ready):
            if item.decision_id not in claimed_ids:
                await _settle(item)
                settled.add(item.decision_id)
        for row in await run_in_db(_read, worker_starts.with_workers):
            if row["id"] not in claimed_ids | settled:
                await _enforce_limit(row)
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
                if row["origin"] and row["origin"].startswith("rule_set:"):
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


def _prepare(row: Mapping[str, Any], run_input: Mapping[str, Any], shipped=None) -> dict[str, Any]:
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
    if shipped is not None:
        if shipped.error or project is None:
            raise inputs.StartRefused("refused: " + (shipped.error or "the shipped workflow needs a project"))
        skill = shipped.declaration.load(project)
    else:
        skill = reachable_workflows(db, owner["id"], project, WORKSPACE).get(row["workflow_id"])
    schema = ((skill.corsia or {}).get("workflow") or {}).get("result") if skill else None
    if skill is None or schema is None:
        raise inputs.StartRefused(f"refused: {row['workflow_id']} is no longer a workflow you can "
                                  "read that declares a valid result")
    facts = (run_input.get("event") or {}).get("facts") or {}
    root = inputs.owner_root(owner["id"], project, db)
    from llm.resolution import resolve_execution_target

    harness = resolve_execution_target(model, db=db).harness
    spawn_extra, applied = _worker_settings(skill, dict(row).get("worker_profile"), model, model_row,
                                            status_row, name, harness, db)
    return {"owner": owner, "model": owner.get("model"),
            "harness": harness, "spawn_extra": spawn_extra, "settings": applied,
            "skill": skill, "schema": schema, "root": root, "facts": facts,
            "workflow": f"{skill.name}@{_workflow_version(skill) or 'unversioned'}",
            "staging": inputs.staging_dir(root, facts.get(inputs.STAGING_FACT)),
            "head": inputs.head_source(project, facts, db)}


#: The larger-context variants a Claude Code model accepts, smallest first; each has no catalog
#: row of its own, so the selector is the model's with the suffix (as the reviewer's main-base run).
_WINDOW_SUFFIXES = ("[1m]",)


def _worker_settings(
    skill: Any, profile: str | None, model: str, model_row: Mapping[str, Any] | None,
    status_row: Mapping[str, Any], name: str, harness: str, db: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """What the workflow's worker settings add to the spawn, and what was applied (the record).

    A workflow with no ``worker`` block adds nothing. Refuses with :class:`worker_inputs.StartRefused`
    when a setting can't be met on the owner's model.
    """
    from core.workflow_worker import InvalidWorker, UnknownProfile, worker_settings

    try:
        settings = worker_settings(skill.corsia, profile)
    except (UnknownProfile, InvalidWorker) as exc:
        raise inputs.StartRefused(f"refused: {skill.name}: {exc}") from exc
    extra: dict[str, Any] = {}
    suffix = _context_suffix(settings["min_context_tokens"], model_row, harness, name, db)
    if suffix:
        extra["window_suffix"] = suffix
    effort = settings["effort"]
    if effort:
        config = (status_row.get("capabilities") or {}).get("reasoning_config") or {}
        values = [str(value) for value in config.get("values") or []]
        if effort not in values:
            takes = f"takes: {', '.join(values)}" if values else "takes no effort setting"
            raise inputs.StartRefused(f"refused: this workflow runs at {effort} effort; your model {name} {takes}. "
                                      "Choose a model that takes it.")
        extra["effort"] = effort
    turns = settings["max_turns"]
    if turns and harness == "claude-code":
        extra["max_turns"] = turns
    applied = {"profile": profile, "model": f"{model}{suffix}", "effort": effort,
               "max_turns": extra.get("max_turns"), "time_limit_minutes": settings["time_limit_minutes"],
               "min_context_tokens": settings["min_context_tokens"]}
    if turns and "max_turns" not in extra:
        applied["max_turns_note"] = f"not applied: {harness} has no per-run turn cap"
    return extra, applied


def _context_suffix(need: int | None, model_row: Mapping[str, Any] | None, harness: str, name: str,
                    db: sqlite3.Connection) -> str:
    """The window suffix the worker's model needs to meet ``need`` tokens (empty: none), or a refusal.

    The effective window is the resolver's (``llm.context_window``), never the catalog's raw
    ``context_window``. A suffixed selector has no row, so the resolver reads a copy of the model's
    row that names the suffix: it then honours the provider's window.
    """
    from llm.context_window import resolve_live_window_facts, resolve_window_facts

    if not need:
        return ""
    effective = resolve_live_window_facts(model_row, db=db).effective if model_row else None
    if effective is not None and effective >= need:
        return ""
    if harness == "claude-code" and model_row:
        for suffix in _WINDOW_SUFFIXES:
            larger = resolve_window_facts(
                {**model_row, "provider_model_id": f"{model_row.get('provider_model_id')}{suffix}"}, db=db)
            if larger.effective is not None and larger.effective >= need:
                return suffix
    has = f"{effective:,} tokens" if effective is not None else "no known context window"
    raise inputs.StartRefused(
        f"refused: this workflow needs a model with at least {need:,} tokens of context; your model "
        f"{name} has {has} and no larger-context version of it can run now.")


async def _enforce_limit(row: Mapping[str, Any]) -> None:
    """Stop a started worker that ran past its workflow's time limit and settle it once as
    ``no_result``. A stop that fails settles nothing: the next pass tries again."""
    check = json.loads(row["run_input"] or "{}")
    settings = check.get("settings") or {}
    limit, began = settings.get("time_limit_minutes"), settings.get("started_at")
    if not limit or not began:
        return
    if datetime.now(timezone.utc) - datetime.fromisoformat(began) < timedelta(minutes=limit):
        return
    try:
        await _stop_worker(row["worker_thread_id"])
    except Exception:  # noqa: BLE001 - a stop that failed is tried again, never settled over
        logger.exception("worker start {} could not be stopped past its limit", row["id"])
        return
    await _end(row, check, NO_RESULT, f"ran past its {limit}-minute limit")


async def _stop_worker(worker: str) -> None:
    """Stop the worker's running turn, if it has one, as its owner would."""
    from infra import state
    from ws.stop_control import do_stop

    running = await run_in_db(_read, _running_turn, worker)
    if running is None:
        return
    turn_id, user_id = running

    async def owns(_user: dict, _sid: str) -> bool:
        return True

    await do_stop({"id": user_id}, worker, state._active_turns.get(worker),
                  state._active_llm_tasks.get(worker), turn_id, owns_thread_fn=owns)


def _running_turn(conn: sqlite3.Connection, worker: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT a.id, t.user_id FROM active_turns a JOIN threads t ON t.id=a.thread_id "
                       "WHERE a.thread_id=? AND a.status='running' ORDER BY a.turn_order DESC LIMIT 1",
                       (worker,)).fetchone()
    return None if row is None else (str(row["id"]), str(row["user_id"]))


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

    from events.connector_workflows import resolve_shipped_workflow

    waited = row["id"] <= _state["waited"]
    try:
        origin = dict(row).get("origin")
        shipped = resolve_shipped_workflow(origin, row["workflow_id"])
        contract = shipped.declaration.result if shipped and shipped.declaration else None
        if contract:
            event_facts = run_input["event"]["facts"]
            run_input["result_context"] = contract.context(origin, row["workflow_id"], event_facts)
            run_input["workflow"] = f"{row['workflow_id']}@{contract.version()}"
        found = await run_in_db(_prepare, row, run_input, shipped)
        if found["harness"] == "codex":
            from subagents.cc_confinement import ConfinementRefused
            from subagents.codex_confinement import verify_version

            try:
                await verify_version()
            except ConfinementRefused as exc:
                raise inputs.StartRefused(str(exc)) from exc
        if contract:
            found["result_instructions"] = contract.instructions()
            found["workflow"] = run_input["workflow"]
            try:
                run_input["result_context"]["record"] = await asyncio.to_thread(
                    contract.capture, Path(found["staging"]),
                )
            except Exception as exc:  # CORSIA input failure, before any worker turn.
                logger.exception("worker start {} could not capture its result record", row["id"])
                raise inputs.StartRefused(f"Couldn’t read the workflow result inputs. {worker_settle.NO_RESULT_NEXT}") from exc
        read_paths, task = await asyncio.to_thread(_write, found, row["id"], run_input)
        check = json.dumps({"workflow": found["workflow"], "schema": found["schema"],
                            "settings": {**found["settings"], "started_at": datetime.now(timezone.utc).isoformat()},
                            "trace": run_input.get("trace") or [], "waited": waited,
                            "item_version": found["facts"].get(inputs.HEAD_SHA_FACT),
                            "result_context": run_input.get("result_context")})

        async def record(worker: str) -> None:
            await run_in_db(_made, row["id"], worker, check)

        started = await _spawn(row["thread_id"], found["owner"], task, {
            "model": found["model"], "confinement": CONFINEMENT_UNTRUSTED,
            "read_paths": read_paths, "title": found["skill"].name, **found["spawn_extra"],
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
