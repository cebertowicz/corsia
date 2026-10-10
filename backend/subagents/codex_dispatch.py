"""Deliver one preflighted input to the Codex runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core.destination_fit import ProviderCallBlocked, require_provider_call_identity
from core.turn_core import TurnIdentity
from infra.send_types import DeliveryFailureSide, SendRejected
from infra.stale_context import stale_rejection
from llm.resolution import ExecutionTarget


PROVIDER = DeliveryFailureSide.PROVIDER
TerminalSettlement = Callable[[str, str | BaseException | None], Awaitable[None]]


def _require_prepared_provider_fit(prepared_context, target: ExecutionTarget) -> None:
    rendered = getattr(prepared_context, "rendered", None)
    ledger = getattr(prepared_context, "fit_ledger", None)
    try:
        require_provider_call_identity(
            ledger,
            harness=target.harness,
            target_catalog_id=target.catalog_id,
            accounting_payload=getattr(rendered, "accounting_payload", None),
            input_identity=getattr(rendered, "fit_input_identity", "") or "",
            current_input=getattr(rendered, "fit_current_input", None),
        )
    except ProviderCallBlocked as exc:
        raise SendRejected(
            409, f"provider destination fit ledger rejected: {exc}"
        ) from exc


def _provider_refused_input(exc: BaseException, cause: str | None) -> bool:
    from subagents.provider_context_limits import is_provider_context_limit

    return cause is not None or is_provider_context_limit(str(exc))


async def _invoke_codex(
    *,
    thread_id: str,
    thread_row: Any,
    content: str,
    klass: str,
    has_thread: bool,
    metadata: dict | None,
    target: ExecutionTarget,
    successor_turn_id: str,
    current_input_message_id: str | None,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]],
    effort: str | None,
    broadcast,
    settle_delivery,
    transfer_delivery_to_turn,
    settle_delivery_terminal,
    mark_accepted,
    mark_ambiguous,
    prepared_context,
    refresh_prepared_context,
    preadmitted_turn,
    media_projection,
):
    from subagents import codex_runtime

    common = {
        "effort": effort,
        "broadcast": broadcast,
        "target": target,
        "successor_turn_id": successor_turn_id,
        "current_input_message_id": current_input_message_id,
        "bind_input_turn": bind_input_turn,
        "settle_delivery": settle_delivery,
        "transfer_delivery_to_turn": transfer_delivery_to_turn,
        "settle_delivery_terminal": settle_delivery_terminal,
        "mark_accepted": mark_accepted,
        "mark_ambiguous": mark_ambiguous,
        "prepared_context": prepared_context,
        "refresh_prepared_context": refresh_prepared_context,
        "preadmitted_turn": preadmitted_turn,
        "media_projection": media_projection,
    }
    if klass == "codex_sub" and has_thread:
        if metadata is not None:
            common["agent_meta"] = metadata
        return (
            "codex_runtime.feed",
            "Codex runtime rejected the resume turn.",
            await codex_runtime.feed(
                thread_id,
                content,
                project_id=thread_row["project_id"],
                **common,
            ),
        )
    return (
        "codex_runtime.feed_main",
        "Codex runtime rejected the reconstructed turn.",
        await codex_runtime.feed_main(
            thread_id,
            content,
            project_id=thread_row["project_id"],
            **common,
        ),
    )


async def _invoke_codex_checked(**kwargs):
    from subagents import codex_runtime, codex_runtime_errors
    from subagents.codex_app_server import CodexAppServerError

    try:
        return await _invoke_codex(**kwargs)
    except codex_runtime.CodexConnectionUnavailable as exc:
        raise SendRejected(503, str(exc), side=PROVIDER) from exc
    except codex_runtime_errors.CodexPreparedContextStale as exc:
        raise stale_rejection(
            exc, harness="codex", label="Codex",
            thread_id=kwargs["thread_id"], turn_id=kwargs["successor_turn_id"],
        ) from exc
    except CodexAppServerError as exc:
        from subagents.codex_historical_image_recovery import (
            classify_historical_image_rejection,
        )

        cause = classify_historical_image_rejection(
            exc,
            fresh_attachments=kwargs["media_projection"] is not None,
        )
        refused_input = _provider_refused_input(exc, cause)
        raise SendRejected(
            502,
            f"Codex turn failed: {exc}",
            metadata={"cause": cause} if cause is not None else None,
            side=PROVIDER if refused_input else None,
            input_undeliverable=refused_input,
        ) from exc
    except ValueError as exc:
        detail = str(exc)
        if "Project working directory unresolved" not in detail:
            raise
        raise SendRejected(
            422,
            f"Codex turn blocked: {detail}",
            metadata={
                "event_type": "harness.error",
                "harness": "codex",
                "cause": "project_working_directory_unresolved",
                "repair_owner": "ctx-stability",
            },
        ) from exc


async def dispatch_codex(
    thread_id: str,
    thread_row: Any,
    content: str,
    klass: str,
    metadata: dict | None,
    *,
    target: ExecutionTarget,
    successor_turn_id: str,
    current_input_message_id: str | None = None,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]] | None,
    allow_fresh_start: bool = False,
    effort: str | None = None,
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
    from infra import state
    from infra.send_dispatch import _validate_project_working_dir
    from subagents import codex_runtime

    _require_prepared_provider_fit(prepared_context, target)
    try:
        codex_runtime.ensure_connection_available()
    except codex_runtime.CodexConnectionUnavailable as exc:
        raise SendRejected(503, str(exc), side=PROVIDER) from exc
    _validate_project_working_dir(thread_row["project_id"], harness="Codex")
    if bind_input_turn is None:
        raise SendRejected(500, "Codex dispatch missing input Turn binder")
    has_thread = await codex_runtime.has_thread_id(thread_id)
    if (
        not has_thread
        and not allow_fresh_start
        and await codex_runtime.has_prior_messages(thread_id)
    ):
        raise SendRejected(
            409,
            "Codex thread has transcript history and no resume handle, and this "
            "dispatch did not authorize a fresh reconstructed Codex start.",
        )
    driver, reject_reason, turn_id = await _invoke_codex_checked(
        thread_id=thread_id, thread_row=thread_row, content=content, klass=klass,
        has_thread=has_thread,
        metadata=metadata, target=target, successor_turn_id=successor_turn_id,
        current_input_message_id=current_input_message_id,
        bind_input_turn=bind_input_turn, effort=effort,
        broadcast=state.owner_broadcast(thread_row["user_id"]),
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous, prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        preadmitted_turn=preadmitted_turn, media_projection=media_projection,
    )
    if not isinstance(turn_id, TurnIdentity):
        raise SendRejected(409, reject_reason)
    return driver, turn_id
