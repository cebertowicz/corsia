"""What a workflow's ``worker`` block (``core.workflow_worker``) sets for its rule-started
worker, read at start with the rule's ``worker_profile`` (``automation.worker_runner``).

The worker still starts on the owner's model, never a model the runner picks. ``effort`` must be
one the model lists. ``max_turns`` is passed to Claude Code only (``--max-turns``); another harness
has no per-run turn cap, and the start's record says so. ``min_context_tokens`` is read against the
model's effective window (``llm.context_window``), never the provider's; a Claude Code model that
falls short but whose provider window meets it runs as its larger-context variant (``[1m]``), and
any other shortfall refuses the start, never a silent fallback to the smaller window. What was
applied is kept on the start's row (``settings``), with the time it started: that is how the limit
is checked (:func:`past_limit`), on the runner's passes, so an overrun is stopped on the next pass
after its limit, never by a timer of its own.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from automation import worker_inputs as inputs
from infra.db_async import run_in_db


def past_limit(kept: Mapping[str, Any]) -> int | None:
    """The time limit in minutes when a start's kept check says its worker ran past it, else None."""
    settings = kept.get("settings") or {}
    limit, began = settings.get("time_limit_minutes"), settings.get("started_at")
    if not limit or not began:
        return None
    if datetime.now(timezone.utc) - datetime.fromisoformat(began) < timedelta(minutes=limit):
        return None
    return int(limit)


#: The larger-context variants a Claude Code model accepts, smallest first; each has no catalog
#: row of its own, so the selector is the model's with the suffix (as the reviewer's main-base run).
_WINDOW_SUFFIXES = ("[1m]",)


def settings_for(
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
    suffix = context_suffix(settings["min_context_tokens"], model_row, harness, name, db)
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


def context_suffix(need: int | None, model_row: Mapping[str, Any] | None, harness: str, name: str,
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


async def stop_worker(worker: str) -> None:
    """Stop the worker's running turn, if it has one, as its owner would."""
    from infra import state
    from infra.database import get_db
    from ws.stop_control import do_stop

    running = await run_in_db(lambda: _running_turn(get_db(), worker))
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
