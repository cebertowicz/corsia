from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.destination_fit import ProviderCallBlocked
from core.turn_core import TurnIdentity
from infra.send_project_paths import validate_project_working_dir as _validate_project_working_dir
from infra.send_types import DeliveryFailureSide, SendRejected
from infra.stale_context import stale_rejection
from infra.turn_start_ack import await_native_start
from llm.reasoning_config import effective_reasoning_effort as resolve_reasoning_effort
from llm.resolution import ExecutionTarget
from subagents.codex_dispatch import dispatch_codex

# PROVIDER marks refusal by the external runtime. Prepared-context staleness is
# retryable because rebuilding against current provider truth makes it valid.
# Everything undeclared is ours; retry must not hide an internal defect.
PROVIDER = DeliveryFailureSide.PROVIDER
TerminalSettlement = Callable[[str, str | BaseException | None], Awaitable[None]]


def _media_projection_kwargs(media_projection) -> dict:
    return {} if media_projection is None else {"media_projection": media_projection}


def _resolve_thread_effort(requested, thread_row: Any, target: ExecutionTarget) -> str | None:
    if requested is None and "reasoning_effort" in thread_row.keys():
        requested = thread_row["reasoning_effort"]
    return resolve_reasoning_effort(requested, target.catalog_id)


async def dispatch_cc_sub(
    thread_id: str,
    content: str,
    metadata: dict | None,
    *,
    target: ExecutionTarget,
    turn_id: str,
    input_message_id: str | None = None,
    effort: str | None = None,
    broadcast=None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    prepared_context=None,
    refresh_prepared_context: Callable[[], Awaitable[Any]] | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
) -> tuple[str, TurnIdentity]:
    from subagents import cc_runtime
    from subagents.cc_runtime_helpers import CCWorkingDirMissing

    spawn_failures = (FileNotFoundError, PermissionError, CCWorkingDirMissing)
    try:
        turn_id = await cc_runtime.feed(
            thread_id,
            content,
            target=target,
            turn_id=turn_id, input_message_id=input_message_id,
            effort=effort,
            broadcast=broadcast,
            record=False,
            agent_meta=metadata,
            persist_input_once=persist_input_once,
            settle_delivery=settle_delivery,
            transfer_delivery_to_turn=transfer_delivery_to_turn,
            settle_delivery_terminal=settle_delivery_terminal,
            mark_accepted=mark_accepted, mark_ambiguous=mark_ambiguous,
            prepared_context=prepared_context,
            refresh_prepared_context=refresh_prepared_context,
            preadmitted_turn=preadmitted_turn,
            **_media_projection_kwargs(media_projection),
        )
    except ProviderCallBlocked as exc:
        raise SendRejected(
            409, f"provider destination fit ledger rejected: {exc}"
        ) from exc
    except cc_runtime.CCPreparedDestinationStale as exc:
        raise stale_rejection(
            exc, harness="claude-code", label="Claude Code",
            thread_id=thread_id, turn_id=turn_id,
        ) from exc
    except spawn_failures as exc:
        raise SendRejected(502, f"Claude Code turn failed: {exc}")
    if not isinstance(turn_id, TurnIdentity):
        raise SendRejected(409, "Thread can't be continued as a Claude Code session.")
    return "cc_runtime.feed", turn_id


async def dispatch_cc_main(
    thread_id: str,
    thread_row: Any,
    content: str,
    *,
    target: ExecutionTarget,
    turn_id: str,
    input_message_id: str | None = None,
    effort: str | None = None,
    broadcast=None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    prepared_context=None,
    refresh_prepared_context: Callable[[], Awaitable[Any]] | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
) -> tuple[str, TurnIdentity]:
    from subagents import cc_runtime
    from subagents.cc_runtime_helpers import CCWorkingDirMissing

    spawn_failures = (FileNotFoundError, PermissionError, CCWorkingDirMissing)
    _validate_project_working_dir(thread_row["project_id"], harness="Claude Code")
    try:
        turn_id = await cc_runtime.feed_main(
            thread_id,
            content,
            target=target,
            turn_id=turn_id, input_message_id=input_message_id,
            effort=effort,
            project_id=thread_row["project_id"],
            broadcast=broadcast,
            persist_input_once=persist_input_once,
            settle_delivery=settle_delivery,
            transfer_delivery_to_turn=transfer_delivery_to_turn,
            settle_delivery_terminal=settle_delivery_terminal,
            mark_accepted=mark_accepted, mark_ambiguous=mark_ambiguous,
            prepared_context=prepared_context,
            refresh_prepared_context=refresh_prepared_context,
            preadmitted_turn=preadmitted_turn,
            **_media_projection_kwargs(media_projection),
        )
    except ProviderCallBlocked as exc:
        raise SendRejected(
            409, f"provider destination fit ledger rejected: {exc}"
        ) from exc
    except cc_runtime.CCPreparedDestinationStale as exc:
        raise stale_rejection(
            exc, harness="claude-code", label="Claude Code",
            thread_id=thread_id, turn_id=turn_id,
        ) from exc
    except spawn_failures as exc:
        if isinstance(exc, CCWorkingDirMissing) and "unresolved" in str(exc).lower():
            raise SendRejected(
                422,
                f"Claude Code turn blocked: {exc}",
                metadata={
                    "event_type": "harness.error",
                    "harness": "claude-code",
                    "cause": "project_working_directory_unresolved",
                    "repair_owner": "ctx-stability",
                },
            ) from exc
        raise SendRejected(502, f"Claude Code turn failed: {exc}")
    if not isinstance(turn_id, TurnIdentity):
        raise SendRejected(409, "Thread can't be continued as a Claude Code session.")
    return "cc_runtime.feed_main", turn_id


def _settle_native_provider_acceptance_on_completion(
    turn_task: asyncio.Task,
    provider_acceptance: asyncio.Future[bool] | None,
) -> None:
    """Use task completion only as a fallback provider-delivery proof."""
    if provider_acceptance is None:
        return

    def _settle(completed: asyncio.Task) -> None:
        cancelled = completed.cancelled()
        failure = None if cancelled else completed.exception()
        if provider_acceptance.done():
            return
        accepted = not cancelled and failure is None
        provider_acceptance.set_result(accepted)

    if turn_task.done():
        _settle(turn_task)
    else:
        turn_task.add_done_callback(_settle)


async def dispatch_native_event(
    thread_id: str,
    thread_row: Any,
    content: str,
    metadata: dict | None,
    *,
    target: ExecutionTarget,
    turn_id: str,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    prepared_context=None,
    preadmitted_turn: TurnIdentity | None = None,
    provider_acceptance: asyncio.Future[bool] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    media_projection=None,
) -> tuple[str, asyncio.Task, TurnIdentity]:
    from infra import state
    from ws.turn import execute_turn

    # An event (GitHub / schedule / automation) wakes a thread on behalf of its
    # OWNER. Broadcast through the cross-tab chokepoint bound to that owner so the
    # owner's other drawers light up (spinner on activity, unread/stop on the
    # terminal) — ``broadcast_to_thread`` alone reaches only sockets viewing the
    # thread, which for an event wake is usually nobody.
    owner_id = thread_row["user_id"]

    async def _broadcast(message: dict) -> None:
        state.broadcast_event(thread_id, owner_id, message)

    start_ack = asyncio.get_running_loop().create_future()
    turn_task = asyncio.create_task(
        execute_turn(
            ws=_HeadlessWS(owner_id),
            user={"id": owner_id},
            user_id=owner_id,
            thread_id=thread_id,
            thread_row=thread_row,
            content=content,
            attachments=[],
            is_retry=False,
            target=target,
            ingress="event",
            turn_id=turn_id,
            reasoning_effort=_resolve_thread_effort(None, thread_row, target),
            broadcast=_broadcast,
            step_metadata=metadata,
            persist_input_once=persist_input_once,
            settle_delivery=settle_delivery,
            prepared_context=prepared_context,
            start_ack=start_ack,
            preadmitted_turn=preadmitted_turn,
            provider_acceptance=provider_acceptance,
            settle_delivery_terminal=settle_delivery_terminal,
            media_projection=media_projection,
        )
    )
    if transfer_delivery_to_turn is not None:
        transfer_delivery_to_turn()
    _settle_native_provider_acceptance_on_completion(
        turn_task,
        provider_acceptance,
    )
    identity = await await_native_start(turn_task, start_ack)
    return "turn_engine", turn_task, identity


async def dispatch_native_delegation(
    thread_id: str,
    thread_row: Any,
    content: str,
    user: dict,
    *,
    target: ExecutionTarget,
    turn_id: str,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    prepared_context=None,
    preadmitted_turn: TurnIdentity | None = None,
    attachments: list | None = None,
    reasoning_effort: str | None = None,
    provider_acceptance: asyncio.Future[bool] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    media_projection=None,
) -> tuple[str, asyncio.Task, TurnIdentity]:
    from infra import state
    from ws.turn import execute_turn

    ws = _HeadlessWS(user["id"])
    start_ack = asyncio.get_running_loop().create_future()
    turn_task = asyncio.create_task(
        execute_turn(
            ws=ws,
            user=user,
            user_id=user["id"],
            thread_id=thread_id,
            thread_row=thread_row,
            content=content,
            attachments=attachments or [],
            is_retry=False,
            target=target,
            turn_id=turn_id,
            ingress="delegation",
            reasoning_effort=_resolve_thread_effort(reasoning_effort, thread_row, target),
            persist_input_once=persist_input_once,
            settle_delivery=settle_delivery,
            prepared_context=prepared_context,
            start_ack=start_ack,
            preadmitted_turn=preadmitted_turn,
            provider_acceptance=provider_acceptance,
            settle_delivery_terminal=settle_delivery_terminal,
            media_projection=media_projection,
        )
    )
    if transfer_delivery_to_turn is not None:
        transfer_delivery_to_turn()
    _settle_native_provider_acceptance_on_completion(
        turn_task,
        provider_acceptance,
    )
    identity = await await_native_start(turn_task, start_ack)
    return "turn_engine", turn_task, identity


async def dispatch_native_ws(
    thread_id: str,
    thread_row: Any,
    content: str,
    user: dict,
    ws: Any,
    *,
    target: ExecutionTarget,
    turn_id: str,
    attachments: list,
    is_retry: bool,
    reasoning_effort: str | None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    after_persist_before_dispatch: (
        Callable[[ExecutionTarget], Awaitable[None]] | None
    ) = None,
    prepared_context=None,
    preadmitted_turn: TurnIdentity | None = None,
    provider_acceptance: asyncio.Future[bool] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    media_projection=None,
) -> tuple[str, TurnIdentity]:
    from infra import state
    from ws.turn import execute_turn

    effective_reasoning_effort = _resolve_thread_effort(
        reasoning_effort, thread_row, target,
    )
    start_ack = asyncio.get_running_loop().create_future()
    turn_task = asyncio.create_task(
        execute_turn(
            ws=ws,
            user=user,
            user_id=user["id"],
            thread_id=thread_id,
            thread_row=thread_row,
            content=content,
            attachments=attachments,
            is_retry=is_retry,
            target=target,
            turn_id=turn_id,
            ingress="ws",
            reasoning_effort=effective_reasoning_effort,
            persist_input_once=persist_input_once,
            settle_delivery=settle_delivery,
            after_persist_before_dispatch=after_persist_before_dispatch,
            prepared_context=prepared_context,
            start_ack=start_ack,
            preadmitted_turn=preadmitted_turn,
            provider_acceptance=provider_acceptance,
            settle_delivery_terminal=settle_delivery_terminal,
            media_projection=media_projection,
        )
    )
    if transfer_delivery_to_turn is not None:
        transfer_delivery_to_turn()
    _settle_native_provider_acceptance_on_completion(
        turn_task,
        provider_acceptance,
    )
    identity = await await_native_start(turn_task, start_ack)
    return "turn_engine", identity


class _HeadlessWS:
    def __init__(self, user_id: str):
        self.user_id = user_id

    async def send_json(self, data: dict) -> None:
        return None


# --- The provider table (the spine) ----------------------------------------
#
# Five legacy execution classes collapse onto THREE providers keyed by the
# execution axis (who owns the agent loop) + an `is_subagent` flag — the route
# is by provider only; `is_subagent` selects seed/parent framing inside a
# provider and never widens the route (refactor design D1). `send_service`
# classifies → builds one `DispatchContext` → calls `dispatch(provider, ctx)`,
# replacing the old 6-way if/elif fork.


@dataclass
class DispatchContext:
    """Everything a provider needs to accept one inbound input.

    Built by ``send_service`` after preflight. Native and CC receive the same
    request-local persist-once callback and invoke it at their exact acceptance
    boundary; Codex keeps its pre-persist reconstruction contract.
    """

    thread_id: str
    thread_row: Any
    content: str
    is_subagent: bool
    target: ExecutionTarget
    turn_id: str
    input_message_id: str | None = None
    metadata: dict | None = None
    native_mode: str = "event"
    allow_fresh: bool = False
    user: dict | None = None
    ws: Any | None = None
    attachments: list | None = None
    media_projection: Any | None = None
    is_retry: bool = False
    reasoning_effort: str | None = None
    persist_input_once: Callable[[], Awaitable[None]] | None = None
    settle_delivery: Callable[[], Awaitable[None]] | None = None
    bind_input_turn: Callable[[str, str | None], Awaitable[None]] | None = None
    after_persist_before_dispatch: (
        Callable[[ExecutionTarget], Awaitable[None]] | None
    ) = None
    mark_accepted: Callable[[], None] | None = None
    mark_ambiguous: Callable[[], None] | None = None
    prepared_context: Any | None = None
    refresh_prepared_context: Callable[[], Awaitable[Any]] | None = None
    preadmitted_turn: TurnIdentity | None = None
    provider_acceptance: asyncio.Future[bool] | None = None
    transfer_delivery_to_turn: Callable[[], None] | None = None
    settle_delivery_terminal: TerminalSettlement | None = None


# execution_class (from runtime_truth) → (provider, is_subagent). `native` is
# never a subagent. Used by `send_service` to pick a provider + framing flag.
_PROVIDER_OF: dict[str, tuple[str, bool]] = {
    "cc_sub": ("cc", True),
    "cc_main": ("cc", False),
    "codex_sub": ("codex", True),
    "codex_main": ("codex", False),
    "native": ("native", False),
}


def provider_for(klass: str) -> tuple[str, bool]:
    """Map an execution class to (provider, is_subagent); fail-loud on unknown."""
    try:
        return _PROVIDER_OF[klass]
    except KeyError:
        raise SendRejected(500, f"unknown execution class {klass!r}")


async def _accept_cc(
    ctx: DispatchContext,
) -> tuple[str, None, TurnIdentity | None]:
    """CC provider: resolve effort, then subagent vs main-chat."""
    from infra.effort import resolve_effort

    effort = resolve_effort(ctx.reasoning_effort, "cc")
    if ctx.target.harness != "claude-code":
        raise SendRejected(500, "Claude Code dispatch received a non-CC target")
    from connections.models import connection_available

    if not connection_available("claude-code"):
        raise SendRejected(
            503,
            "Claude Code connection is not available",
            side=PROVIDER,
        )
    from infra import state

    bcast = state.owner_broadcast(ctx.thread_row["user_id"])
    if ctx.is_subagent:
        driver, turn_id = await dispatch_cc_sub(
            ctx.thread_id,
            ctx.content,
            ctx.metadata,
            target=ctx.target,
            turn_id=ctx.turn_id, input_message_id=ctx.input_message_id,
            effort=effort,
            broadcast=bcast,
            persist_input_once=ctx.persist_input_once,
            settle_delivery=ctx.settle_delivery,
            transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
            settle_delivery_terminal=ctx.settle_delivery_terminal,
            mark_accepted=ctx.mark_accepted,
            mark_ambiguous=ctx.mark_ambiguous,
            prepared_context=ctx.prepared_context,
            refresh_prepared_context=ctx.refresh_prepared_context,
            preadmitted_turn=ctx.preadmitted_turn,
            **_media_projection_kwargs(ctx.media_projection),
        )
    else:
        driver, turn_id = await dispatch_cc_main(
            ctx.thread_id,
            ctx.thread_row,
            ctx.content,
            target=ctx.target,
            turn_id=ctx.turn_id, input_message_id=ctx.input_message_id,
            effort=effort,
            broadcast=bcast,
            persist_input_once=ctx.persist_input_once,
            settle_delivery=ctx.settle_delivery,
            transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
            settle_delivery_terminal=ctx.settle_delivery_terminal,
            mark_accepted=ctx.mark_accepted,
            mark_ambiguous=ctx.mark_ambiguous,
            prepared_context=ctx.prepared_context,
            refresh_prepared_context=ctx.refresh_prepared_context,
            preadmitted_turn=ctx.preadmitted_turn,
            **_media_projection_kwargs(ctx.media_projection),
        )
    return driver, None, turn_id


async def _accept_codex(
    ctx: DispatchContext,
) -> tuple[str, None, TurnIdentity | None]:
    """Codex provider: resolve effort, then resume-vs-fresh via `allow_fresh`."""
    from infra.effort import resolve_effort

    effort = resolve_effort(ctx.reasoning_effort, "codex")
    if ctx.target.harness != "codex":
        raise SendRejected(500, "Codex dispatch received a non-Codex target")
    klass = "codex_sub" if ctx.is_subagent else "codex_main"
    driver, turn_id = await dispatch_codex(
        ctx.thread_id,
        ctx.thread_row,
        ctx.content,
        klass,
        ctx.metadata,
        target=ctx.target,
        successor_turn_id=ctx.turn_id,
        current_input_message_id=ctx.input_message_id,
        bind_input_turn=ctx.bind_input_turn,
        allow_fresh_start=ctx.allow_fresh,
        effort=effort,
        settle_delivery=ctx.settle_delivery,
        transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
        settle_delivery_terminal=ctx.settle_delivery_terminal,
        mark_accepted=ctx.mark_accepted,
        mark_ambiguous=ctx.mark_ambiguous,
        prepared_context=ctx.prepared_context,
        refresh_prepared_context=ctx.refresh_prepared_context,
        preadmitted_turn=ctx.preadmitted_turn,
        media_projection=ctx.media_projection,
    )
    return driver, None, turn_id


async def _accept_native(
    ctx: DispatchContext,
) -> tuple[str, asyncio.Task | None, TurnIdentity | None]:
    """Native provider: routed event vs interactive ws vs delegated headless."""
    if ctx.native_mode == "event":
        driver, turn_task, identity = await dispatch_native_event(
            ctx.thread_id,
            ctx.thread_row,
            ctx.content,
            ctx.metadata,
            target=ctx.target,
            turn_id=ctx.turn_id,
            persist_input_once=ctx.persist_input_once,
            settle_delivery=ctx.settle_delivery,
            prepared_context=ctx.prepared_context,
            preadmitted_turn=ctx.preadmitted_turn,
            provider_acceptance=ctx.provider_acceptance,
            transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
            settle_delivery_terminal=ctx.settle_delivery_terminal,
            media_projection=ctx.media_projection,
        )
        return driver, turn_task, identity
    if ctx.native_mode == "ws":
        if ctx.user is None:
            raise SendRejected(500, "Native WebSocket send missing user context")
        if ctx.ws is None:
            raise SendRejected(500, "Native WebSocket send missing socket context")
        driver, identity = await dispatch_native_ws(
            ctx.thread_id,
            ctx.thread_row,
            ctx.content,
            ctx.user,
            ctx.ws,
            target=ctx.target,
            turn_id=ctx.turn_id,
            attachments=ctx.attachments or [],
            is_retry=ctx.is_retry,
            reasoning_effort=ctx.reasoning_effort,
            persist_input_once=ctx.persist_input_once,
            settle_delivery=ctx.settle_delivery,
            after_persist_before_dispatch=ctx.after_persist_before_dispatch,
            prepared_context=ctx.prepared_context,
            preadmitted_turn=ctx.preadmitted_turn,
            provider_acceptance=ctx.provider_acceptance,
            transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
            settle_delivery_terminal=ctx.settle_delivery_terminal,
            media_projection=ctx.media_projection,
        )
        return driver, None, identity
    if ctx.user is None:
        raise SendRejected(500, "Delegated native send missing user context")
    return await dispatch_native_delegation(
        ctx.thread_id,
        ctx.thread_row,
        ctx.content,
        ctx.user,
        target=ctx.target,
        turn_id=ctx.turn_id,
        persist_input_once=ctx.persist_input_once,
        settle_delivery=ctx.settle_delivery,
        preadmitted_turn=ctx.preadmitted_turn,
        attachments=ctx.attachments,
        reasoning_effort=ctx.reasoning_effort,
        prepared_context=ctx.prepared_context,
        provider_acceptance=ctx.provider_acceptance,
        transfer_delivery_to_turn=ctx.transfer_delivery_to_turn,
        settle_delivery_terminal=ctx.settle_delivery_terminal,
        media_projection=ctx.media_projection,
    )


_PROVIDERS = {"cc": _accept_cc, "codex": _accept_codex, "native": _accept_native}


async def dispatch(
    provider: str,
    ctx: DispatchContext,
) -> tuple[str, Any, TurnIdentity | None]:
    """Accept one input via the named provider; returns (driver, turn_task?).

    The single dispatch entry the spine calls — replaces the per-class fork.
    `turn_task` is non-None only for native delegation (the headless turn task
    the caller tracks); every other provider returns None.
    """
    accept = _PROVIDERS.get(provider)
    if accept is None:
        raise SendRejected(500, f"unknown provider {provider!r}")
    return await accept(ctx)
