"""Prepare durable lineage and runtime context for Claude Code spawn."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from infra.database import write_transaction
from infra.db_async import run_in_db
from core.installed_context import persist_installed_delivery
from llm.resolution import ExecutionTarget
from subagents.cc_accept import _source_artifact
from subagents.cc_context_reconstruction import build_cc_context_reconstruction
from subagents.cc_invocation import (
    CONFINEMENT_NONE,
    ClaudeInvocation,
    permission_mode_for,
)
from subagents.cc_runtime_helpers import (
    _build_seed,
    _check_live_cap,
    _parent_user_id,
    _write_seed_file,
)
from subagents.cc_runtime_state import Broadcast, _Live
from subagents.child_thread import create_child_thread, record_seed


@dataclass(frozen=True)
class CCSpawnRequest:
    parent_thread_id: str
    task: str | None
    binary: str
    target: ExecutionTarget
    working_dir: str | None
    effort: str | None
    title: str | None
    project_id: str | None
    claude_dir: str | None
    broadcast: Broadcast | None
    account_id: str | None = None
    confinement: str = CONFINEMENT_NONE
    add_dirs: tuple[str, ...] = ()


@dataclass
class PreparedCCSpawn:
    request: CCSpawnRequest
    child_id: str
    name: str
    working_dir: str | None
    parent_agent_metadata: dict
    bcast: Broadcast
    live: _Live
    tool_registry: object
    resolved_claude_dir: str
    cc_alias: str


def _parent_context(request: CCSpawnRequest, db) -> tuple[dict, Broadcast]:
    user_id = _parent_user_id(request.parent_thread_id, db)
    row = db.execute(
        "SELECT title,project_id FROM threads WHERE id=?",
        (request.parent_thread_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Claude Code spawn parent thread does not exist")
    metadata = {
        "sender_kind": "agent",
        "from_thread": request.parent_thread_id,
        "from_title": str(row["title"] or ""),
        "scope": row["project_id"] or "workspace",
    }
    from infra import state

    return metadata, request.broadcast or state.owner_broadcast(user_id)


def _validate_target(request: CCSpawnRequest) -> str:
    target = request.target
    if target.harness != "claude-code" or not target.transport_model:
        raise ValueError("Claude Code spawn requires an exact Claude Code target")
    if not target.available:
        raise ValueError(
            "Claude Code target unavailable: "
            f"{target.availability_reason or 'unknown'}"
        )
    return target.transport_model


def _register_lineage(
    request: CCSpawnRequest,
    *,
    user_id: str,
    child_id: str,
    name: str,
) -> int:
    from infra import thread_confinement

    with write_transaction(None, scope="cc_spawn_child") as connection:
        create_child_thread(
            user_id=user_id,
            parent_thread_id=request.parent_thread_id,
            thread_id=child_id,
            title=name,
            model=request.target.catalog_id,
            project_id=request.project_id,
            account_id=request.account_id,
            db=connection,
        )
        # On the row too, so no later model switch launches it unconfined.
        if request.confinement != CONFINEMENT_NONE:
            thread_confinement.record(connection, child_id, request.confinement, request.add_dirs)
    from host_access.supervisor import register_host_access_descendant

    register_host_access_descendant(
        parent_thread_id=request.parent_thread_id,
        thread_id=child_id,
        user_id=user_id,
    )
    from infra.database import get_db

    db = get_db()
    try:
        return int(
            db.execute(
                "SELECT runtime_context_epoch FROM threads WHERE id=?", (child_id,)
            ).fetchone()["runtime_context_epoch"]
        )
    finally:
        db.close()



def _persist_seed(child_id: str, seed_text: str, delivery) -> None:
    """Record the child's seed and the context delivery it installs, in one write."""
    with write_transaction(None, scope="cc_spawn_seed") as connection:
        seed_id = record_seed(child_id, seed_text, db=connection)
        if delivery is not None and delivery.boundary_id is None:
            # The seed is the new child's first record, so its install boundary.
            delivery = replace(delivery, boundary_id=seed_id)
        persist_installed_delivery(delivery, db=connection)


async def _build_live(
    request: CCSpawnRequest,
    db,
    *,
    child_id: str,
    name: str,
    working_dir: str | None,
    cc_alias: str,
    tool_registry,
    runtime_context_epoch: int,
) -> tuple[_Live, str]:
    from subagents.harness_tool_bridge import dynamic_tool_definitions
    from subagents.cc_sessions import resolve_claude_dir
    from connections.account_runtime import thread_claude_dir

    reconstruction = build_cc_context_reconstruction(
        thread_id=child_id,
        base_seed=_build_seed(working_dir),
        catalog_model_id=request.target.catalog_id,
        model_alias=cc_alias,
        working_dir=working_dir,
        project_id=request.project_id,
        is_subagent=True,
        db=db,
    )
    seed_file = _write_seed_file(child_id, reconstruction.seed_text)
    await run_in_db(
        _persist_seed, child_id, reconstruction.persisted_seed_text, reconstruction.installed,
    )
    artifacts = [_source_artifact(
        method="cc-seed",
        snapshot_id=reconstruction.snapshot_id,
        payload={"seed_file": seed_file, "seed_text": reconstruction.seed_text},
    )]
    resolved_dir = (resolve_claude_dir(request.claude_dir) if request.claude_dir is not None
                    else thread_claude_dir(child_id, db=db))
    live = _Live(
        session=None, seed_file=seed_file, claude_dir=resolved_dir,
        parent_thread_id=request.parent_thread_id,
        name=name, model=cc_alias, catalog_model_id=request.target.catalog_id,
        harness=request.target.harness,
        runtime_context_epoch=runtime_context_epoch,
        context_window=request.target.context_window,
        discovered_context_window=request.target.discovered_context_window,
        expected_reported_model=request.target.expected_reported_model,
        context_snapshot_id=reconstruction.snapshot_id,
        context_snapshot=reconstruction.snapshot.to_dict(),
        source_artifacts=artifacts,
        tool_definitions=(
            () if tool_registry is None else dynamic_tool_definitions(tool_registry)
        ),
    )
    return live, resolved_dir


def _confined_record(
    request: CCSpawnRequest, child_id: str, working_dir: str | None, cc_alias: str,
) -> ClaudeInvocation | None:
    """Store an untrusted child's confinement before anything can launch it.

    Without this, a child whose first launch never ran would cold-start
    later from a record with no confinement, so with every tool.
    """
    if request.confinement == CONFINEMENT_NONE:
        return None
    return ClaudeInvocation(
        binary=request.binary,
        permission_mode=permission_mode_for(request.confinement),
        session_id=child_id, working_dir=working_dir, model=cc_alias,
        effort=request.effort, confinement=request.confinement,
        add_dirs=request.add_dirs,
    )


async def prepare_spawn(request: CCSpawnRequest, db) -> PreparedCCSpawn:
    """Create the durable child and its pre-start runtime context."""
    _check_live_cap()
    from connections.models import connection_available
    from registry.cc_session import notify_cc_session, persist_cc_session

    if not connection_available("claude-code", db=db):
        raise ValueError("Claude Code connection is not available")
    parent_metadata, bcast = _parent_context(request, db)
    working_dir = request.working_dir
    cc_alias = _validate_target(request)
    child_id = str(uuid.uuid4())
    if request.confinement != CONFINEMENT_NONE:
        from subagents.cc_confinement import untrusted_working_dir

        working_dir = untrusted_working_dir(child_id, request.project_id, db)
    name = request.title or request.task[:60]
    from subagents.harness_tool_bridge import build_tool_registry

    # An untrusted child has no tool bridge, so no CORSIA tools, as on every later launch.
    tool_registry = None if request.confinement != CONFINEMENT_NONE else build_tool_registry(working_dir)
    user_id = _parent_user_id(request.parent_thread_id, db)
    epoch = await run_in_db(
        _register_lineage,
        request, user_id=user_id, child_id=child_id, name=name,
    )
    _previous_id, session_row = await run_in_db(
        persist_cc_session,
        child_id, child_id, title=name, cwd=working_dir,
        invocation=_confined_record(request, child_id, working_dir, cc_alias),
    )
    notify_cc_session(child_id, child_id, session_row)
    live, resolved_dir = await _build_live(
        request, db, child_id=child_id, name=name,
        working_dir=working_dir, cc_alias=cc_alias,
        tool_registry=tool_registry, runtime_context_epoch=epoch,
    )
    return PreparedCCSpawn(
        request=request, child_id=child_id, name=name,
        working_dir=working_dir,
        parent_agent_metadata=parent_metadata, bcast=bcast, live=live,
        tool_registry=tool_registry, resolved_claude_dir=resolved_dir,
        cc_alias=cc_alias,
    )
