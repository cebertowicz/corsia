"""Construct and configure one exact Codex app-server turn."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.checkpoint_wait import CheckpointWaitScope
from core.continuity_accounting import ContinuityAccounting
from core.turn_core import TurnIdentity
from llm.context_window import WindowFacts
from llm.resolution import Harness
from subagents.codex_runtime_errors import CodexPreparedContextStale
from subagents.codex_runtime_state import (
    _APP_SESSIONS,
    _CODEX_THREADS,
    _LIVE,
    _LiveCodex,
)
from subagents.codex_compaction_retire import retire_compacting_handle
from subagents.codex_runtime_support import codex_request_source_artifacts

log = logging.getLogger(__name__)
InputTerminalSettlement = Callable[[str, str | BaseException | None], Awaitable[None]]

_COMPACTION_SCOPE = "model_auto_compact_token_limit_scope=body_after_prefix"


@dataclass(frozen=True)
class AppServerTurnSpec:
    thread_id: str
    text: str
    current_user_text: str
    successor_turn_id: str
    codex_thread_id: str | None
    binary: str
    working_dir: str | None
    broadcast: object
    project_id: str | None
    effort: str | None
    context_snapshot: dict | None
    continuity_accounting: ContinuityAccounting
    history_items: tuple[dict[str, object], ...]
    tool_registry: object
    dynamic_tools: tuple[dict[str, object], ...] | None
    model: str | None
    catalog_model_id: str | None
    runtime_context_epoch: int
    windows: WindowFacts
    expected_reported_model: str | None
    mark_accepted: Callable[[], None] | None
    mark_ambiguous: Callable[[], None] | None
    settle_delivery: Callable[[], Awaitable[None]] | None
    transfer_delivery_to_turn: Callable[[], None] | None
    settle_delivery_terminal: InputTerminalSettlement | None
    preadmitted_turn: TurnIdentity | None
    media_projection: object
    prepared_context: object
    refresh_prepared_context: Callable[[], Awaitable[object]] | None
    db: object
    # The transition lock generation this turn holds; only it may publish.
    held_lock: asyncio.Lock
    continuing_wait_scope: CheckpointWaitScope | None = None
    client_message_id: str | None = None  # the anchor row, as clientUserMessageId
    harness: Harness = "codex"


def stamp_app_server_context(session, spec: AppServerTurnSpec) -> None:
    """Record the exact catalog context used by the accepted provider call."""
    session.catalog_model_id = spec.catalog_model_id
    session.runtime_context_epoch = spec.runtime_context_epoch
    session.context_snapshot_id = (spec.context_snapshot or {}).get("snapshot_id")
    session.expected_reported_model = spec.expected_reported_model
    session.provider_reported_model = None


def checkpoint_replay_id(prepared_context) -> str | None:
    """Return only a checkpoint-tail replay's durable generation identity."""
    if getattr(prepared_context, "mode", None) != "checkpoint_tail":
        return None
    return getattr(getattr(prepared_context, "replay", None), "checkpoint_generation_id", None)


def reusable_app_session(thread_id: str):
    session = _APP_SESSIONS.get(thread_id)
    if session is not None and getattr(session, "returncode", None) is not None:
        _APP_SESSIONS.pop(thread_id, None)
        return None
    return session


def _compaction_overrides(
    context_window: int | None, *, discovered_context_window: int | None = None,
) -> tuple[str, str]:
    """Compaction-trigger overrides from one resolved target's two windows (#1868)."""
    if (
        not isinstance(context_window, int)
        or isinstance(context_window, bool)
        or context_window <= 0
    ):
        raise ValueError("Codex compaction requires a resolved context window")
    if discovered_context_window is not None:
        # This changes only the compaction trigger, never the model window.
        context_window = discovered_context_window
    return (
        _COMPACTION_SCOPE,
        f"model_auto_compact_token_limit={context_window}",
    )


def _correlated_compaction(live, session, event: dict) -> bool:
    return (
        live.session is session
        and live.corsia_turn is not None
        and event.get("provider_thread_id") == getattr(session, "codex_thread_id", None)
        and event.get("provider_turn_id") == getattr(
            session, "active_turn_id", None
        )
        and bool(event.get("provider_thread_id"))
        and bool(event.get("provider_turn_id"))
    )


def _observe_compaction_retirement(task: asyncio.Task, thread_id: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "codex_runtime: compaction retirement failed for %s: %s",
            thread_id,
            error,
        )


def compaction_event_handler(*, thread_id: str, live, session, downstream):
    """Retire one correlated compaction attempt outside its stdout reader."""

    async def on_event(event: dict) -> None:
        kind = event.get("kind")
        if kind not in {"context_compaction_started", "provider_compaction_breach"}:
            if downstream is not None:
                await downstream(event)
            return
        if not _correlated_compaction(live, session, event):
            log.warning(
                "codex_runtime: ignored stale compaction event for %s",
                thread_id,
            )
            return
        if kind == "provider_compaction_breach":
            log.error("codex_runtime: provider compaction breached for %s", thread_id)
        task = asyncio.create_task(
            retire_compacting_handle(thread_id, live, session),
            name=f"codex-compaction-retire:{thread_id}",
        )
        task.add_done_callback(
            lambda done: _observe_compaction_retirement(done, thread_id)
        )
        await asyncio.sleep(0)

    return on_event


def _set_compaction_callbacks(thread_id, live, session, on_event, on_exit) -> None:
    handler = compaction_event_handler(
        thread_id=thread_id,
        live=live,
        session=session,
        downstream=on_event,
    )
    session.set_callbacks(on_event=handler, on_exit=on_exit)


def record_ambiguous_input(
    on_ambiguous: Callable[[bool], None],
    drain_confirmed: bool,
) -> None:
    """Record ambiguous acceptance without masking its transport failure."""
    try:
        on_ambiguous(drain_confirmed)
    except Exception:
        log.exception("codex_runtime: ambiguous-input bookkeeping failed")


async def causal_app_server_request(
    start_input: Callable[[Callable[[], None], Callable[[], None]], Awaitable[object]],
    *,
    on_ambiguous: Callable[[bool], None],
    on_write: Callable[[], None] | None = None,
) -> object:
    """Run one input request and report only non-definitive post-write failure."""
    write_observed = False
    drain_confirmed = False

    def on_write_observed() -> None:
        nonlocal write_observed
        write_observed = True
        if on_write is not None:
            on_write()

    def on_drain_confirmed() -> None:
        nonlocal drain_confirmed
        drain_confirmed = True

    try:
        return await start_input(on_write_observed, on_drain_confirmed)
    except BaseException as exc:
        from subagents.codex_app_server_errors import CodexAppServerError

        explicit_provider_error = (
            isinstance(exc, CodexAppServerError) and "code" in exc.payload
        )
        if write_observed and not explicit_provider_error:
            record_ambiguous_input(on_ambiguous, drain_confirmed)
        raise


def resolve_app_server_tools(
    session,
    *,
    working_dir: str | None,
    tool_registry,
    dynamic_tools: tuple[dict[str, object], ...] | None,
    capabilities: frozenset[str],
):
    if tool_registry is None:
        if dynamic_tools is not None:
            raise ValueError("Codex dynamic tools require their exact CORSIA registry")
        from subagents.harness_tool_bridge import (
            build_tool_registry,
            dynamic_tool_definitions,
        )

        tool_registry = build_tool_registry(working_dir)
        dynamic_tools = dynamic_tool_definitions(tool_registry, capabilities)
        return tool_registry, dynamic_tools
    return validate_prepared_app_server_tools(session, tool_registry, dynamic_tools)


def validate_prepared_app_server_tools(session, tool_registry, dynamic_tools):
    """Reuse the exact surface already frozen during preflight."""
    if dynamic_tools is None:
        raise ValueError("Codex prepared registry requires exact dynamic tool definitions")
    if session is not None and (
        getattr(session, "tool_registry", None) is not tool_registry
        or tuple(getattr(session, "dynamic_tools", ()) or ()) != dynamic_tools
    ):
        raise CodexPreparedContextStale(
            "Codex provider tool surface changed after context preflight"
        )
    return tool_registry, dynamic_tools


def new_app_server_live(
    session,
    *,
    codex_thread_id: str | None,
    model: str | None,
    catalog_model_id: str | None,
    runtime_context_epoch: int,
    windows: WindowFacts,
    expected_reported_model: str | None,
    context_snapshot: dict | None,
    harness: Harness = "codex",
    continuity_accounting: ContinuityAccounting,
    continuing_wait_scope: CheckpointWaitScope | None = None,
) -> _LiveCodex:
    live = _LiveCodex(
        codex_thread_id=codex_thread_id or getattr(session, "codex_thread_id", "") or "",
        session=session,
        parent_thread_id=None,
        name="codex",
        model=model,
        catalog_model_id=catalog_model_id,
        harness=harness,
        runtime_context_epoch=runtime_context_epoch,
        context_window=windows.effective,
        discovered_context_window=windows.discovered,
        expected_reported_model=expected_reported_model,
        provider_reported_model=None,
        context_snapshot_id=(context_snapshot or {}).get("snapshot_id"),
        context_snapshot=context_snapshot,
        continuity_accounting=continuity_accounting,
        requires_terminal_identity=True,
    )
    if continuing_wait_scope is not None:
        live.checkpoint_wait_scope = continuing_wait_scope
    return live


def capture_app_server_request(live: _LiveCodex, session) -> None:
    """Attach the JSON-RPC proof for one accepted app-server request."""
    live.source_artifacts = codex_request_source_artifacts(
        session,
        live.context_snapshot_id,
    )


async def configure_app_server_input(
    session,
    *,
    live: _LiveCodex,
    text: str,
    binary: str,
    codex_thread_id: str | None,
    working_dir: str | None,
    exact_model: str | None,
    effort: str | None,
    history_items: tuple[dict[str, object], ...],
    dynamic_tools,
    tool_registry,
    thread_id: str,
    project_id: str | None,
    on_event,
    on_exit,
    media_projection,
    client_message_id: str | None = None,
):
    from infra.db_async import run_in_db

    config_overrides = await run_in_db(
        _compaction_overrides, live.context_window,
        discovered_context_window=live.discovered_context_window,
    )
    if session is None:
        from subagents.codex_app_server import CodexAppServerSession

        session = CodexAppServerSession(
            binary=binary,
            task=text,
            codex_thread_id=codex_thread_id or None,
            working_dir=working_dir,
            model=exact_model,
            catalog_model_id=getattr(live, "catalog_model_id", None),
            effort=effort,
            history_items=history_items,
            dynamic_tools=dynamic_tools,
            tool_registry=tool_registry,
            corsia_thread_id=thread_id,
            project_id=project_id,
            config_overrides=config_overrides,
            on_event=None,
            on_exit=on_exit,
        )
        live.bind_app_server_session(session)
        _set_compaction_callbacks(thread_id, live, session, on_event, on_exit)
        session.corsia_turn_identity = live.corsia_turn

        async def start_input(on_write, on_drain):
            return await session.start(
                on_turn_write_observed=on_write,
                on_turn_drain_confirmed=on_drain,
                media_projection=media_projection, client_message_id=client_message_id,
            )
    else:
        if tuple(getattr(session, "config_overrides", ())) != config_overrides:
            raise CodexPreparedContextStale(
                "Codex context window changed after app-server spawn"
            )
        _set_compaction_callbacks(thread_id, live, session, on_event, on_exit)
        live.bind_app_server_session(session)
        session.catalog_model_id = getattr(live, "catalog_model_id", None)
        turn_kwargs = {"model": exact_model} if exact_model else {}

        async def start_input(on_write, on_drain):
            return await session.start_turn(
                text,
                **turn_kwargs,
                media_projection=media_projection,
                on_write_observed=on_write,
                on_drain_confirmed=on_drain, client_message_id=client_message_id,
            )

    return session, start_input


def remember_spawned_turn(
    spec: AppServerTurnSpec,
    live: _LiveCodex,
    session,
) -> None:
    if getattr(session, "codex_thread_id", None):
        live.codex_thread_id = session.codex_thread_id
        _CODEX_THREADS[spec.thread_id] = session.codex_thread_id
    log.info(
        "codex_runtime: spawned app-server turn session=%s thread_id=%s",
        spec.thread_id,
        live.codex_thread_id or "(fresh)",
    )
