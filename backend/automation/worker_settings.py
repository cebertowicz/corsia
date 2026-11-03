"""What a workflow's ``worker`` block (``core.workflow_worker``) sets for its rule-started
worker, read at start with the rule's ``worker_profile`` (``automation.worker_runner``).

The worker starts on the owner's model, or on that model's larger-context variant, and otherwise
refuses: the runner never picks another model. ``effort`` must be one the model lists.
``max_turns`` is passed to Claude Code only (``--max-turns``); another harness has no per-run turn
cap, and the start's record says so. ``min_context_tokens`` is read against the model's effective
window (``llm.context_window``); a Claude Code model that falls short runs as its ``[1m]`` catalog
row when that row passes the dispatch gate every delegation passes and its own window meets it.
Any other shortfall refuses the start, never a silent fallback to the smaller window. What was
applied is kept on the start's row (``settings``) while it is open, with the time it started: that
is how the limit is checked (:func:`past_limit`), on the runner's passes, so an overrun is stopped
on the next pass after its limit, never by a timer of its own. Settling ends that record.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

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


def settings_for(
    skill: Any, profile: str | None, model: str, model_row: Mapping[str, Any] | None,
    status_row: Mapping[str, Any], name: str, harness: str, scope: Mapping[str, Any],
    db: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """What the workflow's worker settings add to the spawn, and what was applied (the record).

    A workflow with no ``worker`` block adds nothing. Refuses with :class:`worker_inputs.StartRefused`
    when a setting can't be met on the owner's model.
    """
    from core.workflow_worker import InvalidWorker, UnknownProfile, worker_settings

    try:
        settings = worker_settings(skill.corsia, profile)
    except UnknownProfile as exc:
        raise inputs.StartRefused(
            f"refused: this rule starts the {skill.name} workflow with the worker profile {profile}, "
            "which that workflow does not have. Change the rule's profile or add it to the workflow.") from exc
    except InvalidWorker as exc:
        raise inputs.StartRefused(
            f"refused: the worker settings in the {skill.name} workflow are not valid. "
            "Fix them in the workflow.") from exc
    extra: dict[str, Any] = {}
    larger = larger_context_model(settings["min_context_tokens"], model_row, harness, name, scope, db)
    if larger:
        extra["model"] = larger
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
    applied = {"profile": profile, "model": larger or model, "effort": effort,
               "max_turns": extra.get("max_turns"), "time_limit_minutes": settings["time_limit_minutes"],
               "min_context_tokens": settings["min_context_tokens"]}
    if turns and "max_turns" not in extra:
        applied["max_turns_note"] = f"not applied: {harness} has no per-run turn cap"
    return extra, applied


def larger_context_model(need: int | None, model_row: Mapping[str, Any] | None, harness: str, name: str,
                         scope: Mapping[str, Any], db: sqlite3.Connection) -> str | None:
    """The catalog id of the larger-context variant to run instead (None: the owner's model as it is),
    or a refusal.

    The effective window is the resolver's (``llm.context_window``), never the catalog's raw
    ``context_window``. The variant is the catalog's own ``[1m]`` row, resolved as the reviewer
    resolves its main-base model and gated as every delegation is; its window is its own row's.
    """
    from llm.context_window import resolve_live_window_facts
    from llm.delegation_model import eligible_delegation_model
    from llm.resolution import ModelUnavailableError, catalog_row, resolve_harness_selector

    if not need:
        return None
    effective = resolve_live_window_facts(model_row, db=db).effective if model_row else None
    if effective is not None and effective >= need:
        return None
    if harness == "claude-code" and model_row:
        try:
            variant = resolve_harness_selector("claude-code", f"{model_row.get('provider_model_id')}[1m]", db=db)
        except (ModelUnavailableError, ValueError):
            variant = None
        if variant is not None:
            catalog_id, refusal = eligible_delegation_model(variant.catalog_id, **scope)
            row = catalog_row(catalog_id, db=db) if catalog_id and refusal is None else None
            window = resolve_live_window_facts(row, db=db).effective if row else None
            if window is not None and window >= need:
                return catalog_id
    has = f"{effective:,} tokens" if effective is not None else "no known context window"
    raise inputs.StartRefused(
        f"refused: this workflow needs a model with at least {need:,} tokens of context; your model "
        f"{name} has {has} and no larger-context version of it can run now. "
        "Choose a model with a larger context for this chat.")


async def stop_worker(worker: str) -> bool:
    """Stop the worker's running turn; True only when that turn ended by this stop.

    A worker with no running turn (its first turn still queued, say) has nothing to stop, and any
    other outcome of the one system Stop, or a Stop that fails, is not proof the turn ended: all
    return False, and the caller tries again on its next pass.
    """
    from infra.database import get_db
    from ws.stop_control import stop_turn_for_cause

    try:
        turn_id = await run_in_db(lambda: _running_turn(get_db(), worker))
        if turn_id is None:
            return False
        return (await stop_turn_for_cause(worker, turn_id, plan_limit=False)).outcome == "stopped"
    except Exception:  # noqa: BLE001 - a stop that failed is tried again, never settled over
        logger.exception("worker {} could not be stopped past its limit", worker)
        return False


def _running_turn(conn: sqlite3.Connection, worker: str) -> str | None:
    row = conn.execute("SELECT id FROM active_turns WHERE thread_id=? AND status='running' "
                       "ORDER BY turn_order DESC LIMIT 1", (worker,)).fetchone()
    return None if row is None else str(row["id"])
