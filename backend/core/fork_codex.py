"""One disposable Codex fork over a frozen rollout prefix."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from history.types import HistoryBoundary
from core.fork_account import admit_fork, bind_fork_session

from core.fork_runtime import ForkKind
from llm.resolution import ExecutionTarget
from core.fork_codex_result import (
    CodexForkCapture,
)

from core.fork_types import (
    ProviderForkRejected,
    WarmForkUnusable,
    ProviderForkRequest,
    ProviderForkResult,
)
from core.fork_types import ColdInputCallback
from core.fork_codex_root import registered_rollout as _registered_rollout
from core.fork_capture import warm_capture_refused


@dataclass(frozen=True, slots=True)
class CodexWarmSource:
    """A durable Codex rollout prefix and its ordinary launch settings."""

    thread_id: str
    boundary: HistoryBoundary
    provider_thread_id: str
    rollout_path: str
    rollout_bytes: int
    rollout_sha256: str
    sessions_root: str
    working_dir: str
    reasoning_effort: str
    vendor_account_receipt: object | None = None


def _sessions_root(path: Path) -> Path | None:
    return next((parent for parent in path.parents if parent.name == "sessions"), None)


def warm_tool_boundary_prefix_bytes(
    thread_id: str,
    boundary: Any,
) -> int | str:
    """Cut the registered rollout through its CORSIA boundary, or say why not."""
    registered = _registered_rollout(thread_id)
    if registered is None:
        return "rollout_unregistered"
    if isinstance(registered, str):
        return registered
    rollout = registered.rollout
    try:
        from llm.codex_rollout_boundary import codex_rollout_boundary_cut

        with Path(rollout).open("rb") as stream:
            return codex_rollout_boundary_cut(stream, boundary)
    except OSError:
        return "rollout_unreadable"


def _warm_source_proof(thread_id: str, prefix_bytes: int | None):
    registered = _registered_rollout(thread_id)
    if registered is None:
        return "rollout_unregistered"
    if isinstance(registered, str):
        return registered
    provider_id, rollout, effort = registered.provider_id, registered.rollout, registered.effort
    rollout = Path(rollout)
    sessions_root = _sessions_root(rollout)
    if sessions_root is None:
        return "rollout_outside_sessions"
    try:
        from llm.codex_rollout_files import codex_rollout_prefix_proof

        size, digest, metadata, _ = codex_rollout_prefix_proof(rollout, prefix_bytes)
    except (OSError, RuntimeError):
        return "rollout_unreadable"
    cwd = metadata.get("cwd")
    if metadata.get("id") != provider_id or not isinstance(cwd, str) or not cwd:
        return "rollout_identity_mismatch"
    return provider_id, rollout, effort, sessions_root, size, digest, cwd, registered.receipt


async def capture_warm_source(
    thread_id: str,
    boundary: HistoryBoundary,
    *,
    target: ExecutionTarget,
    binary: str,
    prefix_bytes: int | None = None,
) -> CodexWarmSource | None:
    """Freeze exactly the rollout prefix present at the safe boundary."""
    del binary
    if target.harness != "codex":
        return warm_capture_refused(thread_id, "codex", "target_not_codex")
    if boundary.thread_id != thread_id:
        return warm_capture_refused(thread_id, "codex", "boundary_thread_mismatch")
    proof = await asyncio.to_thread(_warm_source_proof, thread_id, prefix_bytes)
    if isinstance(proof, str):
        return warm_capture_refused(thread_id, "codex", proof)
    provider_id, rollout, effort, sessions_root, size, digest, cwd, receipt = proof
    return CodexWarmSource(
        thread_id,
        boundary,
        provider_id,
        str(rollout),
        size,
        digest,
        str(sessions_root),
        cwd,
        effort,
        receipt,
    )


async def _capture_tool_boundary_warm_source(
    context: Any,
    live: Any,
    thread_id: str,
):
    """Read an immutable rollout prefix and recheck the live handle after I/O."""
    if context.target.harness != "codex":
        return warm_capture_refused(thread_id, "codex", "target_not_codex")
    if context.boundary is None:
        return warm_capture_refused(thread_id, "codex", "no_boundary")
    if context.warm_boundary is None:
        return warm_capture_refused(thread_id, "codex", "no_warm_boundary")
    from core.fork_render import capture_warm_fork_source
    from subagents.codex_runtime_state import _LIVE

    if refused := _live_refusal(thread_id, live, _LIVE):
        return warm_capture_refused(thread_id, "codex", refused)
    prefix_bytes = await asyncio.to_thread(
        warm_tool_boundary_prefix_bytes,
        thread_id,
        context.warm_boundary,
    )
    if isinstance(prefix_bytes, str):
        return warm_capture_refused(thread_id, "codex", prefix_bytes)
    if refused := _live_refusal(thread_id, live, _LIVE):
        return warm_capture_refused(thread_id, "codex", refused)
    # capture_warm_fork_source logs its own reason for every refusal.
    source = await capture_warm_fork_source(
        thread_id,
        context.boundary,
        target=context.target,
        binary=context.binary,
        reasoning_effort=context.reasoning_effort,
        live=live,
        prefix_bytes=prefix_bytes,
    )
    if source is None:
        return None
    if refused := _live_refusal(thread_id, live, _LIVE):
        return warm_capture_refused(thread_id, "codex", refused)
    return source


def _live_refusal(thread_id: str, live: Any, registry: Any) -> str | None:
    """Name why this live handle can no longer own a warm capture, if it cannot."""
    if registry.get(thread_id) is not live:
        return "live_changed"
    return "tool_calls_outstanding" if live.tool_step_ids else None


def _copy_rollout(source: CodexWarmSource, isolated_home: Path) -> None:
    from llm.codex_rollout_files import copy_codex_rollout_to_isolated_home

    copy_codex_rollout_to_isolated_home(
        Path(source.rollout_path),
        source_thread_id=source.provider_thread_id,
        source_sessions_root=Path(source.sessions_root),
        isolated_home=isolated_home,
        source_prefix_bytes=source.rollout_bytes,
    )


def _fork_tools(request: ProviderForkRequest, cwd: str, kind: ForkKind):
    """Use the exact cold surface frozen at fit, or rebuild warm callbacks."""
    from core.fork_tools import allowed_definitions
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions
    from tools.catalog import fork_tool_definitions

    if request.warm_source is not None:
        # A warm resume keeps the source's tool list so its cached prefix
        # holds; the kind's allowlist is enforced at execution instead.
        registry = request.tool_registry or build_tool_registry(cwd)
        return registry, dynamic_tool_definitions(registry)
    if request.tool_registry is not None:
        definitions = (
            fork_tool_definitions(request.tool_registry, kind.tool_allowlist)
            if hasattr(request.tool_registry, "get_definitions") else request.dynamic_tool_specs
        )
        return request.tool_registry, allowed_definitions(definitions, kind)
    registry = build_tool_registry(cwd)
    definitions = allowed_definitions(
        fork_tool_definitions(registry, kind.tool_allowlist)
        if hasattr(registry, "get_definitions") else dynamic_tool_definitions(registry),
        kind,
    )
    return registry, definitions


def _fork_session(
    request: ProviderForkRequest,
    source: CodexWarmSource | None,
    *,
    source_home: Path,
    isolated_home: Path,
    cwd: str,
    on_event: Any,
    on_exit: Any,
    cold_input=None,
    fork_tool_result_fits=None,
    kind: ForkKind,
    cancel_event: asyncio.Event | None = None,
    on_raw_message=None,
):
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_spawn import _compaction_overrides

    from core.fork_tools import ForkToolBoundary

    registry, definitions = _fork_tools(request, cwd, kind)
    boundary = ForkToolBoundary(
        kind, request, registry, cancel_event or asyncio.Event(), fork_tool_result_fits,
    )
    return bind_fork_session(request, CodexAppServerSession(
        binary=request.binary or "",
        task=cold_input.as_prompt(request.instruction) if cold_input else request.instruction,
        codex_thread_id=source.provider_thread_id if source is not None else None,
        working_dir=cwd,
        model=request.target.transport_model,
        effort=source.reasoning_effort if source else request.reasoning_effort,
        dynamic_tools=definitions,
        tool_registry=registry,
        corsia_thread_id=request.thread_id,
        fork_id=request.fork_id,
        on_raw_message=on_raw_message,
        # Managed tool receipts read the rollout, and Codex gives an ephemeral
        # thread none. The temporary isolated home keeps the writer disposable.
        ephemeral=False,
        allow_provider_model_fallback=False,
        developer_instructions=request.developer_instructions,
        codex_home=str(isolated_home),
        source_codex_home=str(source_home),
        config_overrides=_compaction_overrides(
            request.target.context_window,
            discovered_context_window=request.target.discovered_context_window,
        ),
        on_event=on_event,
        on_exit=on_exit,
        fork_tool_result_fits=boundary,
    ), kind=kind)


def _fit_cold_request(request: ProviderForkRequest, cwd: str, *, kind: ForkKind):
    from core.fork_render import codex_cold_surface, fit_cold_fork_input

    registry, definitions = _fork_tools(request, cwd, kind)
    surface, registry, definitions, developer = codex_cold_surface(
        cwd, writer=request.target, registry=registry, definitions=definitions,
        developer=request.developer_instructions,
    )
    request = replace(
        request, tool_registry=registry, dynamic_tool_specs=definitions,
        developer_instructions=developer,
    )
    return request, fit_cold_fork_input(request, provider_surface_tokens=surface)


async def _run_app_server_fork(
    request: ProviderForkRequest,
    source: CodexWarmSource | None,
    *,
    source_home: Path, isolated_home: Path,
    cwd: str, cancel_event: asyncio.Event,
    cold_input=None,
    kind: ForkKind,
) -> ProviderForkResult:
    from core.fork_codex_context import CodexForkContext, frozen_context_tokens
    from core.token_estimation import measure_request

    if source is None:
        if cold_input is None:
            raise ValueError(f"cold {kind.name} requires its fitted input")
        initial_tokens = cold_input.input_tokens
    else:
        initial_tokens = await asyncio.to_thread(
            frozen_context_tokens, Path(source.rollout_path), source.rollout_bytes,
            target=request.target, fallback_tokens=measure_request({
                "tools": request.dynamic_tool_specs,
                "developer_instructions": request.developer_instructions,
            }, target=request.target).tokens,
        )
        initial_tokens += measure_request(request.instruction, target=request.target).tokens
    context = CodexForkContext(request.target, initial_tokens)
    capture = CodexForkCapture(on_usage=request.on_usage, kind=kind)

    async def tool_boundary(reply, usage=None):
        if usage is not None:
            context.record_receipt(usage)
        fits = context.fits if reply is None else context.accept_tool_result(reply)
        if not fits:
            capture.truncate()
        return fits

    if cancel_event.is_set():
        raise asyncio.CancelledError
    warm_room = source is None or context.leaves_output_room(request.max_output_tokens)
    if not (context.fits and warm_room):
        # Only a warm fork can be corrected to cold; a cold overflow is terminal.
        refusal = WarmForkUnusable if source is not None else ProviderForkRejected
        raise refusal(f"Codex {kind.name} initial input exceeds selected context window")

    from core.fork_codex_control import CompletedItemLimit

    item_limit = CompletedItemLimit(kind, request)

    async def on_raw_message(frame):
        await item_limit.observe(session, frame)

    async def on_event(event):
        if event.get("kind") != "call_usage" or event.get("turn_id") == session.active_turn_id:
            await capture.on_event(event)

    session = _fork_session(
        request,
        source,
        source_home=source_home,
        isolated_home=isolated_home,
        cwd=cwd,
        on_event=on_event,
        on_exit=capture.on_exit,
        cold_input=cold_input,
        fork_tool_result_fits=tool_boundary,
        kind=kind,
        cancel_event=cancel_event,
        on_raw_message=on_raw_message,
    )
    from core.fork_codex_control import run_session

    return await run_session(session, capture, item_limit, cancel_event)


async def invoke_fork(
    request: ProviderForkRequest,
    cancel_event: asyncio.Event,
    *,
    kind: ForkKind,
    on_cold_input: ColdInputCallback | None = None,
) -> ProviderForkResult:
    """Run one disposable fork through the Codex process."""
    if request.target.harness != "codex":
        raise TypeError("Codex adapter requires a Codex target")
    request = admit_fork(request)
    source = request.warm_source
    if source is not None and not isinstance(source, CodexWarmSource):
        raise TypeError(f"Codex {kind.name} received another harness's warm source")
    if cancel_event.is_set():
        raise asyncio.CancelledError
    cold_input = None
    if source is None:
        source_home = Path(request.vendor_launch.codex_source or request.vendor_launch.warm_home)
        cwd = request.working_dir
        request, cold_input = await asyncio.to_thread(_fit_cold_request, request, cwd, kind=kind)
        if on_cold_input is not None:
            await on_cold_input(cold_input.first_included_seq)
    else:
        source_home = Path(source.sessions_root).parent
        cwd = source.working_dir
    from connections.account_operation import operation_directory, require_same_operation_home

    require_same_operation_home(request, source_home if source else None)
    with operation_directory(
        request, prefix='corsia-codex-fork-', kind=kind.name,
    ) as (request, isolated_home):
        if source is not None:
            _copy_rollout(source, isolated_home)
        return await _run_app_server_fork(
            request,
            source,
            source_home=source_home,
            isolated_home=isolated_home,
            cwd=cwd,
            cancel_event=cancel_event,
            cold_input=cold_input,
            kind=kind,
        )
