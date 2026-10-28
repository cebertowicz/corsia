"""Support helpers for the Codex runtime bridge."""

from __future__ import annotations

import inspect
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from infra.database import get_db
from infra.db_async import run_in_db
from subagents.codex_runtime_state import (
    _APP_SESSIONS,
    _CODEX_THREADS,
    _LIVE,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexOnEventContext:
    """Values the lifted Codex event handlers close over."""
    live: Any
    thread_id: Any
    bcast: Any
    dead_set: Any
    thread_registry: Any
    bind_tool_result_echo: Any
    read_bound_tool_result: Any
    load_assistant_step_display: Any
    persist_assistant_text: Any
    persist_tool_use: Any


def reasoning_representation_kind(
    current: dict | None,
    incoming: dict,
) -> str:
    """Preserve summary evidence when a later Codex item is opaque."""
    existing = current if isinstance(current, dict) else {}
    kinds = {
        provenance.get("representation_kind")
        for provenance in (existing, incoming)
    }
    if "codex_reasoning_summary" in kinds:
        return "codex_reasoning_summary"
    if "opaque" in kinds:
        return "opaque"
    return incoming.get("representation_kind", "unknown")


def request_stop_checkpoint_wait(live) -> None:
    """Release only the hard waiter carried by this exact Turn handle."""
    if live is None:
        return
    live.checkpoint_wait_scope.request_stop()


def quarantined_unknown_write(thread_id: str, message_id: str) -> bool:
    """Return whether this message ended with an unproven provider write."""
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT 1 FROM messages WHERE id=? AND thread_id=? "
            "AND delivery_state='ambiguous' "
            "AND delivery_error='provider_write_outcome_unknown' LIMIT 1",
            (message_id, thread_id),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def turn_has_durable_terminal(turn_id: str) -> bool:
    """Return whether this Codex Turn has durably left active state."""
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT status FROM active_turns WHERE id=?", (turn_id,)
        ).fetchone()
        return row is not None and row["status"] in {
            "completed", "cancelled", "error",
        }
    finally:
        connection.close()


def resolve_working_dir(project_id: str | None, db) -> str:
    if project_id:
        if db is None:
            raise ValueError(
                f"Project working directory unresolved for project_id={project_id}"
            )
        from core.project_context import resolve_project_paths
        from infra.config import WORKSPACE

        _project_name, workspace_dir = resolve_project_paths(
            WORKSPACE,
            project_id,
            db=db,
        )
        if workspace_dir:
            return str(workspace_dir)
        raise ValueError(
            f"Project working directory unresolved for project_id={project_id}"
        )
    from infra.config import WORKSPACE

    return str(WORKSPACE)


def thread_runtime_context_epoch(thread_id: str, db=None) -> int:
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT runtime_context_epoch FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return int(row["runtime_context_epoch"]) if row else 0
    finally:
        if not db:
            connection.close()


def working_dir_for_reconstruction(
    project_id: str | None,
    db=None,
) -> str | None:
    if not project_id:
        return resolve_working_dir(None, None)
    connection = db or get_db()
    try:
        return resolve_working_dir(project_id, connection)
    finally:
        if not db:
            connection.close()


def thread_registration_metadata(
    session, rollout_path: str | None, checkpoint_generation_id: str | None = None,
) -> dict:
    """Persist only launch facts needed to resume the provider thread."""
    metadata = {
        "rollout_path": rollout_path,
        "reasoning_effort": str(getattr(session, "effort", None) or "").strip(),
    }
    if checkpoint_generation_id is not None:
        metadata["checkpoint_generation_id"] = checkpoint_generation_id
    return metadata


def _metadata(value) -> dict:
    """Decode one registry metadata value without treating corruption as fresh."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def codex_thread_registration(
    thread_id: str,
    db,
) -> tuple[str | None, str | None, str | None]:
    """Return provider id, rollout location, and launch effort."""
    from registry import models

    row = models.find_for_thread(thread_id, "codex", "thread", db=db)
    provider_id = (row or {}).get("external_id")
    if not isinstance(provider_id, str) or not provider_id:
        return None, None, None
    metadata = _metadata((row or {}).get("metadata"))
    corruption = metadata.pop("registry_metadata_error", None)
    if corruption:
        log.warning(
            "codex checkpoint: unreadable registry metadata on codex/thread "
            "artifact %s (thread %s): %s; ignoring its recorded rollout path",
            (row or {}).get("id"),
            thread_id,
            corruption,
        )
        metadata = {}
    rollout_path = metadata.get("rollout_path")
    effort = metadata.get("reasoning_effort")
    return (
        provider_id,
        rollout_path if isinstance(rollout_path, str) and rollout_path else None,
        effort if isinstance(effort, str) else None,
    )


def codex_thread_reasoning_effort(
    thread_id: str,
    provider_id: str,
    registered_effort: str | None,
    selected_effort: str,
) -> str:
    """Resolve the effort that launched the exact registered provider thread."""
    live = _LIVE.get(thread_id)
    session = live.session if live is not None else _APP_SESSIONS.get(thread_id)
    session_thread = getattr(live, "codex_thread_id", None) or getattr(
        session, "codex_thread_id", None
    )
    if (
        session_thread == provider_id
        and getattr(session, "returncode", None) is None
        and hasattr(session, "effort")
    ):
        return str(session.effort or "").strip()
    return registered_effort if registered_effort is not None else selected_effort


async def restore_thread_id(
    thread_id: str,
    db,
    *,
    validate_tool_surface: bool = True,
) -> str | None:
    """Restore the durable provider thread without policing local tool state."""
    del validate_tool_surface
    from registry import models

    def _lookup():
        _db = db or get_db()
        try:
            row = models.find_for_thread(thread_id, "codex", "thread", db=_db)
            if row is None:
                return None
            return row.get("external_id")
        finally:
            if not db:
                _db.close()

    codex_thread_id = await run_in_db(_lookup)
    if codex_thread_id:
        log.info(
            "codex_runtime: restored thread_id=%s from registry for session=%s",
            codex_thread_id,
            thread_id,
        )
        _CODEX_THREADS[thread_id] = codex_thread_id
    return codex_thread_id


async def has_thread_id(thread_id: str, db=None) -> bool:
    return bool(
        _CODEX_THREADS.get(thread_id) or await restore_thread_id(thread_id, db)
    )


async def has_prior_messages(thread_id: str, db=None) -> bool:
    def _lookup():
        _db = db or get_db()
        try:
            row = _db.execute(
                "SELECT 1 FROM messages WHERE thread_id = ? "
                "AND delivery_state = 'delivered' LIMIT 1",
                (thread_id,),
            ).fetchone()
            return row is not None
        finally:
            if not db:
                _db.close()

    return bool(await run_in_db(_lookup))


def async_method(obj: object | None, name: str):
    method = getattr(obj, name, None)
    if method is None or not inspect.iscoroutinefunction(method):
        return None
    return method


def is_stale_stop_live(current, turn_id: str, expected_live=None) -> bool:
    """Return whether Stop now observes a successor instead of its target."""
    return current is not None and (
        (expected_live is not None and current is not expected_live)
        or getattr(current.corsia_turn, "turn_id", turn_id) != turn_id
    )


def exact_stop_live(thread_id: str, turn_id: str, expected_live=None):
    """Return only the exact live handle captured for one Stop request."""
    live = _LIVE.get(thread_id)
    if live is None or (expected_live is not None and live is not expected_live):
        return None
    return live if getattr(live.corsia_turn, "turn_id", None) == turn_id else None


def request_exact_stop(thread_id: str, turn_id: str, expected_live=None):
    """Synchronously halt exact Codex work and close new request admission."""
    live = exact_stop_live(thread_id, turn_id, expected_live)
    if live is None:
        return None
    session = live.session
    request_terminate = getattr(session, "_request_terminate", None)
    revoke_admission = getattr(session, "revoke_dynamic_tool_admission", None)
    wait_requests_idle = async_method(session, "wait_dynamic_tool_requests_idle")
    if not callable(request_terminate) or not callable(revoke_admission):
        return None
    if wait_requests_idle is None:
        return None
    live.provider_context_untrusted = True
    request_terminate()
    revoke_admission()
    return live, session, wait_requests_idle


async def terminate_app_session(thread_id: str) -> bool:
    session = _APP_SESSIONS.get(thread_id)
    if session is None:
        return True
    live = _LIVE.get(thread_id)
    if live is not None and live.session is session:
        return True
    terminate = async_method(session, "terminate")
    if terminate is None:
        return False
    await terminate()
    if _APP_SESSIONS.get(thread_id) is not session:
        return False
    _APP_SESSIONS.pop(thread_id, None)
    return True


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def codex_request_source_artifacts(
    session,
    snapshot_id: str | None,
) -> list[dict]:
    """Freeze the accepted JSON-RPC requests proving one provider write."""
    methods = {
        "thread/start",
        "thread/resume",
        "thread/inject_items",
        "turn/start",
        "turn/steer",
    }
    artifacts: list[dict] = []
    for entry in getattr(session, "request_log", []) or []:
        method = entry.get("method")
        if method not in methods:
            continue
        params = entry.get("params") or {}
        artifacts.append(
            {
                "kind": "json-rpc-request",
                "harness": "codex",
                "method": method,
                "request_id": entry.get("id"),
                "snapshot_id": snapshot_id,
                "content_hash": _stable_hash({"method": method, "params": params}),
                "params": params,
            }
        )
    return artifacts
