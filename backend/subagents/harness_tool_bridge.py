"""CORSIA-owned tool bridge for external harness adapters."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from infra import state
from infra.config import WORKSPACE
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.settings import get_tool_config
from infra.tool_result_errors import is_error_result
from subagents.cc_runtime_state import record_tool_token_rotation
from subagents.harness_tool_publication import (
    ProjectedMediaResult,
    ToolBridgeResult,
    publish_completed_tool_result,
)
from tools.registry import ToolRegistry
from tools.recorded_call import (
    CompletedToolCall,
    CompletedToolFailure,
    completed_tool_failure,
    enrich_completed_failure,
)

if TYPE_CHECKING:
    from core._tool_result_delivery_support import ToolExecutionResult
    from core.turn_core import TurnIdentity


def build_tool_registry(working_dir: str | None = None) -> ToolRegistry:
    """Build the same registry shape native turns use for harness tools."""
    from core.agent import CORSIAAgent, _register_continuity_tool, _register_tools

    workspace = Path(working_dir) if working_dir else WORKSPACE
    agent = CORSIAAgent(workspace=workspace)
    _register_tools(agent, get_tool_config(), workspace)
    _register_continuity_tool(agent)
    from tools.mcp_runtime import register_mcp_tools

    register_mcp_tools(agent.tools)
    from core.agent import _apply_eval_tool_allowlist

    _apply_eval_tool_allowlist(agent)
    return agent.tools


def dynamic_tool_definitions(registry: ToolRegistry) -> tuple[dict[str, Any], ...]:
    """Return the advertised tool list for Claude Code (MCP) and Codex (dynamic)."""
    from tools.catalog import advertised_tool_definitions

    return advertised_tool_definitions(registry)


def write_cc_mcp_config(
    *,
    thread_id: str,
    tool_token: str,
    fork_id: str | None = None,
) -> str:
    """Write a Claude Code MCP config that exposes only CORSIA tools."""
    server = Path(__file__).with_name("corsia_mcp_server.py")
    port = os.environ.get("BACKEND_PORT", str(DEFAULT_BACKEND_PORT))
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise RuntimeError(f"Invalid BACKEND_PORT for Claude Code tool bridge: {port!r}")
    env = {
        "CORSIA_CC_TOOL_URL": f"http://127.0.0.1:{port}/api/cc/_tool-bridge/{thread_id}",
        "CORSIA_CC_TOOL_TOKEN": tool_token,
    }
    if fork_id is not None:
        env["CORSIA_FORK_ID"] = fork_id
    payload = {
        "mcpServers": {
            "corsia": {
                "command": sys.executable,
                "args": [str(server)],
                "env": env,
                "alwaysLoad": True,
            }
        }
    }
    out_dir = Path(tempfile.gettempdir()) / "corsia-harness-mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix="cc-",
        suffix=".json",
        dir=out_dir,
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return str(path)


def configure_cc_tool_bridge(
    live: Any,
    *,
    thread_id: str,
    working_dir: str | None,
    registry: ToolRegistry | None = None,
) -> str:
    """Bind one live CC process to the backend-owned tool executor."""
    registry = registry if registry is not None else build_tool_registry(working_dir)
    token = secrets.token_urlsafe(32)
    config = write_cc_mcp_config(thread_id=thread_id, tool_token=token)
    # A replaced-in-place token is a rotation too: remember the predecessor so
    # its in-flight calls survive the dual-accept window (issue #825). Normal
    # transitions revoke first, so this is usually a no-op.
    record_tool_token_rotation(thread_id, getattr(live, "tool_token", "") or "")
    live.tool_registry = registry
    live.tool_token = token
    live.tool_config = config
    return config


async def settle_cc_tool_result(live, thread_id: str, result, identity) -> bool:
    """Preserve the completed result; return whether a checkpoint resumed."""
    try:
        return await _settle_cc_tool_result(live, thread_id, result, identity)
    except CompletedToolFailure:
        raise
    except Exception as error:
        completion = result.completion or CompletedToolCall(
            name=result.delivery.name, result=result.delivery.content,
            arguments=None,
            thread_id=thread_id, turn_id=identity.turn_id if identity else None,
            invocation_id=result.delivery.invocation_id,
            provider_tool_call_id=result.delivery.provider_tool_call_id,
            assistant_step_id=result.delivery.assistant_step_id, canonical=result.delivery,
        )
        raise completed_tool_failure(
            (completion,), error, harness="claude-code",
        ) from error


async def _settle_cc_tool_result(live, thread_id: str, result, identity) -> bool:
    """Count projected CC replies before settling and holding the final Step."""
    from infra.db_async import run_in_db
    from llm.resolution import resolve_execution_target
    from subagents.cc_event_tool_settlement import settle_committed_tool_result
    from subagents.harness_checkpoint_resume import maintain_external_tool_boundary

    session = live.session
    if session is None:
        raise RuntimeError("Claude tool reply lacks its exact active transport")
    target = await run_in_db(resolve_execution_target, live.catalog_model_id)
    await session.record_tool_reply(result.delivery.provider_tool_call_id, result, target)
    if await settle_committed_tool_result(
        live=live, child_id=thread_id, identity=identity,
        canonical=result.delivery, bcast=state.broadcast_to_thread,
    ):
        # Other replies may have counted while this settlement awaited observers.
        return await maintain_external_tool_boundary(
            live, thread_id, "claude-code",
            current_tool_call_id=result.delivery.provider_tool_call_id,
            complete_input_tokens=session.tool_context.tokens,
        )
    return False


async def settle_codex_tool_result(app_session: Any, result: str, reply: dict) -> None:
    """Settle one Codex result, then hold only its final Step boundary."""
    if not isinstance(result, ToolBridgeResult):
        return
    from core._tool_result_delivery_support import ToolResultBindingError
    from subagents.codex_runtime_events import settle_committed_tool_result
    from subagents.codex_runtime_state import _LIVE

    thread_id = app_session.corsia_thread_id
    live = _LIVE.get(thread_id)
    if live is None or live.session is not app_session:
        raise ToolResultBindingError(
            "Codex tool settlement lacks its exact active live handle"
        )
    from infra.db_async import run_in_db
    from llm.resolution import resolve_execution_target

    target = await run_in_db(resolve_execution_target, live.catalog_model_id)
    complete_input = app_session._tool_requests.measure_tool_reply(reply, target)
    identity = result.turn_identity or getattr(app_session, "corsia_turn_identity", None)
    await settle_committed_tool_result(
        live=live,
        thread_id=thread_id,
        identity=identity,
        canonical=result.delivery,
    )
    if not live.tool_step_ids:
        from subagents.harness_checkpoint_resume import (
            maintain_external_tool_boundary,
        )

        await maintain_external_tool_boundary(
            live, thread_id, "codex", complete_input_tokens=complete_input,
        )



async def _media_delivery(delivery: "ToolExecutionResult", model: str | None):
    """Resolve harness media off-loop through the canonical delivery owner."""
    from core.media_delivery import tool_result_media_for_model

    return await asyncio.to_thread(tool_result_media_for_model, delivery, model)


def _persist_delivery(
    *,
    thread_id: str,
    turn_identity: "TurnIdentity",
    invocation_id: str,
    provider_tool_call_id: str | None,
    assistant_step_id: str | None,
    name: str,
    arguments: dict[str, Any],
    content: str,
) -> "ToolExecutionResult":
    """Persist one harness result against its authoritative Assistant Step."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import write_transaction

    with write_transaction(None, scope="persist_harness_tool_result") as db:
        if assistant_step_id is None:
            raise ToolResultBindingError(
                "harness tool execution lacks provider Assistant Step identity"
            )
        media_reference = getattr(content, "media", None)
        delivery = persist_tool_execution_result(
            thread_id=thread_id,
            turn_id=turn_identity.turn_id,
            invocation_id=invocation_id,
            assistant_step_id=assistant_step_id,
            provider_tool_call_id=provider_tool_call_id,
            name=name,
            arguments=arguments,
            content=content,
            is_error=is_error_result(content),
            extra_metadata=(
                {"media": media_reference.as_metadata()}
                if media_reference is not None
                else None
            ),
            db=db,
        )
        return delivery


async def _persist_delivery_off_loop(**delivery: Any) -> "ToolExecutionResult":
    """Persist one harness result in the database pool, off the event loop."""
    from infra.db_async import run_in_db

    return await run_in_db(_persist_delivery, **delivery)


async def _with_notice_and_marker(
    projected: str,
    delivery,
    *,
    thread_id: str,
    turn_identity: "TurnIdentity",
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
    workspace: Path | None = None,
) -> str:
    """Return a result's model text: the projection, plan/repo notices, then the echo marker."""
    from core.plan_notice import plan_notice, with_notice
    from core.repo_staleness_notice import repo_staleness_notice
    from core.tool_result_invocation import with_invocation_reference
    from events.connectors.github_pr_notice import pr_event_notice
    from tools.tool_surface import resolve_dispatch

    # The notice classifies the tool the call dispatches to, not its
    # ``tool_call`` wrapper.
    dispatched_name, _dispatched_args, _dispatcher = resolve_dispatch(name, arguments)
    notice = await asyncio.to_thread(
        plan_notice,
        thread_id=thread_id,
        turn_id=turn_identity.turn_id,
        tool_call_id=provider_tool_call_id,
        tool_name=dispatched_name,
    )
    # The notices sit beside the result, and the echo marker still lands last.
    projected = with_notice(projected, notice)
    repo_notice = await asyncio.to_thread(
        repo_staleness_notice,
        thread_id=thread_id,
        tool_name=name,
        workspace=workspace,
        arguments=arguments,
    )
    projected = with_notice(projected, repo_notice)
    projected = with_notice(projected, await asyncio.to_thread(
        pr_event_notice, thread_id=thread_id, tool_name=name, arguments=arguments,
    ))
    return with_invocation_reference(projected, delivery.invocation_id)


async def execute_tool(
    registry: ToolRegistry,
    *,
    thread_id: str | None,
    turn_identity: TurnIdentity | None = None,
    invocation_id: str | None = None,
    provider_tool_call_id: str | None = None,
    assistant_step_id: str | None = None,
    fork_id: str | None = None,
    fork_policy: Any = None,
    model: str | None = None,
    name: str,
    arguments: dict[str, Any],
    harness: str = "external",
) -> str:
    """Execute once, persist exact bytes, then return a bounded model projection."""
    thread_token = state._current_thread_id.set(thread_id)
    turn_token = state._current_turn_identity.set(turn_identity)
    completion = None
    try:
        with state.tool_invocation_context(invocation_id, fork_id=fork_id,
                                           provider_call_id=provider_tool_call_id):
            if fork_policy is None:
                content = await registry.execute(name, arguments)
            else:
                content = await fork_policy.execute(name, arguments)
        completion = CompletedToolCall(
            name=name, result=content, arguments=dict(arguments), thread_id=thread_id,
            turn_id=turn_identity.turn_id if turn_identity else None,
            invocation_id=invocation_id, provider_tool_call_id=provider_tool_call_id,
            assistant_step_id=assistant_step_id, fork_id=fork_id,
        )
        from tools.tool_surface import resolve_dispatch

        dispatched_name, _dispatched_args, _dispatcher = resolve_dispatch(name, arguments)
        dispatched_tool = registry.get(dispatched_name)
        decorate_result = functools.partial(
            _with_notice_and_marker,
            workspace=dispatched_tool.workspace if dispatched_tool else None,
        )
        return await publish_completed_tool_result(
            completion,
            thread_id=thread_id,
            turn_identity=turn_identity,
            fork_id=fork_id,
            model=model,
            persist_delivery=_persist_delivery_off_loop,
            media_delivery=_media_delivery,
            decorate_result=decorate_result,
            harness=harness,
        )
    except CompletedToolFailure as failure:
        enrich_completed_failure(
            failure,
            name=name,
            arguments=arguments,
            thread_id=thread_id,
            turn_id=turn_identity.turn_id if turn_identity else None,
            invocation_id=invocation_id,
            provider_tool_call_id=provider_tool_call_id,
            assistant_step_id=assistant_step_id,
            fork_id=fork_id,
        )
        raise
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)
