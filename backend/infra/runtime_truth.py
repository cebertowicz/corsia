"""Shared runtime truth for thread status and delegated send validation.

Selected model/provider is the dispatch source of truth. Runtime/resume state
is addressability metadata: useful for diagnosing whether the selected runtime
can continue, but not allowed to override current model intent.
"""

from __future__ import annotations

import json
from typing import Any

from core.checkpoint_types import EAGER_ADOPTION_TRIGGERS
from infra.db_async import run_read
from infra.execution_class import ExecutionClass, execution_class, is_subagent
from infra.thread_liveness import read_thread_liveness
from core.turn_core import active_turn_thread_ids
from llm.resolution import (
    ExecutionTarget,
    ModelUnavailableError,
    resolve_execution_target,
)


def provider_boundary_snapshot(
    thread_id: str, harness: str, model: str, *, db=None,
) -> tuple[Any | None, Any | None, Any | None]:
    """Read raw checkpoint state plus the exact eager-adoption candidate."""
    from infra.checkpoint_queries import active_for_thread, current_installed
    from infra.database import get_db

    connection = db or get_db()
    try:
        if db is None:
            connection.execute("BEGIN")
        thread_row = connection.execute(
            "SELECT history_epoch FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if thread_row is None:
            return None, None, None
        epoch = int(thread_row["history_epoch"])
        active = active_for_thread(thread_id, db=connection)
        if active is not None and active.history_epoch != epoch:
            active = None
        installed = current_installed(thread_id, db=connection)
        eager = installed if installed is not None and (
            installed.trigger in EAGER_ADOPTION_TRIGGERS
            and installed.source_harness == harness
            and installed.source_model == model
        ) else None
        return active, installed, eager
    finally:
        if db is None:
            connection.close()


def registered_checkpoint_id(registration: dict | None) -> str | None:
    """Read one provider registration's exact adopted checkpoint identity."""
    try:
        metadata = json.loads((registration or {}).get("metadata") or "{}")
    except (TypeError, ValueError):
        return None
    value = metadata.get("checkpoint_generation_id")
    return value if isinstance(value, str) and value else None


def _has_registered_provider(
    thread_id: str, integration: str, artifact_type: str, harness: str,
) -> bool:
    from infra.database import get_db
    from registry.models import find_for_thread

    connection = get_db()
    try:
        connection.execute("BEGIN")
        registration = find_for_thread(
            thread_id, integration, artifact_type, db=connection,
        )
        if registration is None:
            return False
        thread_row = connection.execute(
            "SELECT model FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if thread_row is None:
            return True
        target = resolve_execution_target(str(thread_row["model"] or ""), db=connection)
        _, _, eager = provider_boundary_snapshot(
            thread_id, harness, target.catalog_id, db=connection,
        )
        return bool(
            eager is None
            or registered_checkpoint_id(registration) == eager.id
        )
    finally:
        connection.close()


def _has_registered_cc_session(thread_id: str) -> bool:
    return _has_registered_provider(
        thread_id, "claude-code", "session", "claude-code",
    )


def _has_registered_codex_thread(thread_id: str) -> bool:
    return _has_registered_provider(thread_id, "codex", "thread", "codex")


def _field(thread_row: Any, key: str) -> Any:
    if not hasattr(thread_row, "keys"):
        raise TypeError(
            f"runtime_truth: expected sqlite3.Row or dict, got "
            f"{type(thread_row).__name__!r}"
        )
    if key not in thread_row.keys():
        raise KeyError(
            f"runtime_truth: column {key!r} absent from thread row. "
            f"Row keys: {list(thread_row.keys())!r}"
        )
    return thread_row[key]


def driver_for_class(klass: ExecutionClass) -> str:
    """Map an execution class to the runtime driver label exposed in status."""
    if klass in ("codex_main", "codex_sub"):
        return "codex"
    if klass in ("cc_main", "cc_sub"):
        return "cc"
    return "engine"


def _selected_runtime(
    thread_row: Any,
    model: str,
    target: ExecutionTarget | None,
) -> tuple[ExecutionClass | None, str | None, str | None]:
    try:
        resolved = target or (resolve_execution_target(model) if model else None)
    except ModelUnavailableError as exc:
        return None, None, f"{exc.reason}. Choose another model."
    klass = execution_class(thread_row, target=resolved)
    return klass, driver_for_class(klass), None


async def _cc_runtime_truth(thread_id: str, is_cc: bool) -> tuple[bool, bool]:
    if not is_cc:
        return False, False
    from subagents import cc_runtime

    live = cc_runtime.is_live(thread_id)
    has_session = await run_read(_has_registered_cc_session, thread_id)
    return live, has_session


async def _codex_runtime_truth(
    thread_id: str, is_codex: bool, timing=None,
) -> tuple[bool, bool, bool, bool]:
    if not is_codex:
        return False, False, False, False
    from subagents import codex_runtime
    from subagents.codex_runtime_state import provider_context_untrusted

    live = codex_runtime.is_live(thread_id)
    untrusted = provider_context_untrusted(thread_id)
    lookup = _has_registered_codex_thread
    if timing is not None:
        lookup = timing.wrap("codex_runtime_db_lookup", lookup)
    has_thread = await run_read(lookup, thread_id)
    has_prior_messages = False
    if has_thread:
        await codex_runtime.has_thread_id(thread_id)
    elif not live:
        has_prior_messages = await codex_runtime.has_prior_messages(thread_id)
    return live, untrusted, has_thread, has_prior_messages


async def read_runtime_truth(
    thread_id: str,
    thread_row: Any,
    *,
    active_turn: dict | None = None,
    conductor_scopes: list[dict] | None = None,
    target: ExecutionTarget | None = None,
    timing=None,
) -> dict:
    """Return the selected runtime, addressability, and liveness for one thread.

    The returned ``driver`` is derived from ``execution_class(thread_row)`` so
    status and `/send` stay aligned. If the selected model was removed, status
    preserves the selector with null runtime fields and an addressability error;
    dispatch still rejects that error. Codex resume-handle fields remain visible
    diagnostics, but a missing handle is reconstructable from CORSIA DB history
    under the RC-0 context contract.

    ``liveness`` separates "a turn is running" from "no turn is running" from
    "the last turns ran and produced nothing"; see ``infra.thread_liveness``.
    """
    model = _field(thread_row, "model") or ""
    klass, driver, addressability_error = _selected_runtime(
        thread_row, model, target,
    )
    is_cc = klass in ("cc_main", "cc_sub")
    is_codex = klass in ("codex_main", "codex_sub")

    cc_live, cc_has_session = await _cc_runtime_truth(thread_id, is_cc)
    (
        codex_live,
        codex_provider_context_untrusted,
        codex_has_thread,
        codex_has_prior_messages,
    ) = await _codex_runtime_truth(thread_id, is_codex, timing)
    addressable = addressability_error is None

    turn_active = active_turn is not None or thread_id in active_turn_thread_ids()
    if turn_active:
        busy_reason = "active_turn"
    else:
        busy_reason = None

    # ``busy`` only answers whether a turn is running. On its own it cannot tell
    # an idle thread from one whose turns start, end, and produce nothing, so a
    # wedge reports the healthy shape (#947 gap 2).
    liveness = await run_read(
        read_thread_liveness, thread_id, turn_active=turn_active,
    )

    return {
        "model": model,
        "reasoning_effort": (
            _field(thread_row, "reasoning_effort")
            if "reasoning_effort" in thread_row.keys()
            else None
        ),
        "thread_kind": "subagent" if is_subagent(thread_row) else "chat",
        "project_id": _field(thread_row, "project_id"),
        "driver": driver,
        "execution_class": klass,
        "cc_live": cc_live,
        "cc_has_session": cc_has_session,
        "codex_live": codex_live,
        "codex_provider_context_untrusted": codex_provider_context_untrusted,
        "codex_has_thread": codex_has_thread,
        "codex_has_prior_messages": codex_has_prior_messages,
        "busy": turn_active,
        "busy_reason": busy_reason,
        "liveness": liveness.state,
        "unproductive_turns": liveness.unproductive_turns,
        "queued_inputs": liveness.queued_inputs,
        "last_turn": liveness.last_turn,
        "addressable": addressable,
        "addressability_error": addressability_error,
        "conductor_scopes": conductor_scopes or [],
    }
