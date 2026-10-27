"""Codex app-server JSON-RPC transport."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from automation.restart_messages import TURN_STOPPED_REFUSAL
from core.fork_codex_context import CodexForkContext
from infra import provider_usage, turn_volume
from subagents import codex_app_server_events as ev
from subagents import codex_app_server_notifications as notifications
from subagents.codex_app_server_errors import CodexAppServerError
from subagents.codex_app_server_reader import read_app_server
from subagents.codex_tool_requests import ToolRequests
from subagents.codex_process import CodexProcessGroup, cancel_reader_bounded, signal_process
from subagents.codex_dynamic_tools import dynamic_tool_values as _dynamic_tool_values
from subagents.codex_dynamic_tools import dynamic_tool_response as _dynamic_tool_rejection
from subagents.codex_env import governed_codex_model_catalog_path
from subagents.managed_launch import (
    codex_app_server_config_override_args,
    codex_app_server_governance_args,
    codex_developer_instructions_with_write_path,
    codex_sandbox_reads_read_only,
    codex_tools_are_write_capable,
)
from subagents.stream_capture import record_frame

if TYPE_CHECKING:
    from core.turn_core import TurnIdentity

log = logging.getLogger(__name__)

OnEvent = Callable[[dict], Awaitable[None]]
OnExit = Callable[[int | None, str | BaseException], Awaitable[None]]
OnRawMessage = Callable[[dict[str, Any]], Awaitable[None]]
STDOUT_LIMIT = 16 * 1024 * 1024
REQUEST_TIMEOUT = 120.0
STEER_REQUEST_TIMEOUT = 10.0
_STEER_NOT_WRITTEN = object()
_DYNAMIC_TOOL_DRAIN_REJECTION = TURN_STOPPED_REFUSAL
_DYNAMIC_TOOL_IDENTITY_REJECTION = (
    "Error: Codex dynamic tool request does not belong to the active provider turn"
)
# Codex-owned auth refresh is not a model action; answer it as unsupported.
_NON_ACTION_SERVER_REQUESTS = {"account/chatgptAuthTokens/refresh"}


class CodexAppServerSession:
    def __init__(
        self,
        *,
        binary: str,
        task: str | None,
        codex_thread_id: str | None = None,
        working_dir: str | None = None,
        model: str | None = None,
        catalog_model_id: str | None = None,
        effort: str | None = None,
        history_items: tuple[dict[str, Any], ...] | None = None,
        dynamic_tools: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        tool_registry: Any | None = None,
        corsia_thread_id: str | None = None,
        corsia_turn_identity: TurnIdentity | None = None,
        fork_id: str | None = None,
        fork_tool_result_fits: Callable[..., bool | Awaitable[bool]] | None = None,
        project_id: str | None = None,
        sandbox_mode: str = "read-only",
        approval_policy: str = "never",
        ephemeral: bool | None = None,
        allow_provider_model_fallback: bool | None = None,
        developer_instructions: str | None = None,
        codex_home: str | None = None,
        source_codex_home: str | None = None,
        config_overrides: tuple[str, ...] | list[str] | None = None,
        control_only: bool = False,
        on_event: OnEvent | None = None,
        on_exit: OnExit | None = None,
        on_raw_message: OnRawMessage | None = None,
    ) -> None:
        self.transport_generation_id = str(uuid.uuid4())
        self.binary, self.task = binary, task
        self.thread_id = codex_thread_id
        self.working_dir = working_dir
        self.model, self.catalog_model_id = model, catalog_model_id
        self.effort = effort
        self.history_items = tuple(history_items or ())
        self.dynamic_tools = tuple(dynamic_tools or ())
        self.tool_registry = tool_registry
        self.corsia_thread_id, self.corsia_turn_identity = corsia_thread_id, corsia_turn_identity
        self.declared_tool_step_ids: dict[str, str] = {}
        self.fork_id, self.fork_tool_result_fits = fork_id, fork_tool_result_fits
        self._tool_requests = ToolRequests.for_session(self)
        self.tool_context = CodexForkContext(None, None)
        self.project_id = project_id
        self.sandbox_mode = sandbox_mode
        self.approval_policy = approval_policy
        self.ephemeral = ephemeral
        self.allow_provider_model_fallback = allow_provider_model_fallback
        self.developer_instructions = developer_instructions
        self.codex_home, self.source_codex_home = codex_home, source_codex_home
        self.config_overrides = tuple(config_overrides or ())
        self.control_only = control_only
        # Launch flags disable shell/search; thread controls cover residual operations.
        self._on_event = on_event
        self._on_exit = on_exit
        self._on_raw_message = on_raw_message
        self._closing = False
        self._proc: asyncio.subprocess.Process | None = None
        self._process_group: CodexProcessGroup | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._pending_methods: dict[int, str] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._stderr = bytearray()
        self._terminal_error: BaseException | None = None  # Consumed by transport exit.
        self._active_turn_id: str | None = None
        self._completed_turns: dict[str, asyncio.Future] = {}
        self._usage_by_turn: dict[str, dict] = {}
        self._active_turn_started: tuple[str, datetime] | None = None  # (turn id, requested at)
        # Codex reports a thread-cumulative token counter, so a turn's spend is
        # the delta across it. Held separately from ``_usage_by_turn`` so
        # discarding occupancy at a compaction or terminal cannot erase measured spend.
        self._turn_volume = turn_volume.CumulativeTurnVolume()
        self._clear_reasoning_observations()
        self.request_log: list[dict[str, Any]] = []
        self._dynamic_tool_admission_open, self._dynamic_tool_requests_idle = True, asyncio.Event()
        self._dynamic_tool_requests_idle.set()

    @property
    def codex_thread_id(self) -> str | None:
        return self.thread_id

    @property
    def active_turn_id(self) -> str | None:
        """Expose the exact provider Turn currently owned by this session."""
        return self._active_turn_id

    def build_cmd(self) -> list[str]:
        cmd = [self.binary, "app-server", "--stdio", *codex_app_server_governance_args()]
        if not self.control_only:
            catalog_path = governed_codex_model_catalog_path(
                Path(self.codex_home) if self.codex_home else None,
            )
            cmd += [
                "-c",
                f"model_catalog_json={json.dumps(str(catalog_path))}",
            ]
        if self.effort:
            cmd += ["-c", f"model_reasoning_effort={self.effort}"]
        cmd += codex_app_server_config_override_args(self.config_overrides)
        return cmd

    def build_env(self) -> dict[str, str]:
        """Return a clean launch environment with an optional isolated home."""
        from subagents.codex_env import build_session_env

        return build_session_env(self)

    def set_callbacks(
        self, *, on_event: OnEvent | None = None, on_exit: OnExit | None = None
    ) -> None:
        self._on_event = on_event
        self._on_exit = on_exit

    def revoke_dynamic_tool_admission(self) -> None:
        """Reject dynamic requests that have not entered the inline handler."""
        self._dynamic_tool_admission_open = False

    def retain_completed_tool_failure(self, error: BaseException) -> None:
        """Latch ordered completed results for the existing transport-exit owner."""
        from tools.recorded_call import (
            CompletedToolFailure,
            combine_completed_failures,
        )

        if not isinstance(error, CompletedToolFailure):
            raise TypeError("terminal tool failure must retain completed results")
        self._terminal_error = combine_completed_failures(self._terminal_error, error)

    def retain_control_failure(self, error: BaseException) -> None:
        """Keep permanent failure on the same owner used by admission and exit."""
        if self._terminal_error is None:
            self._terminal_error = error

    def restore_dynamic_tool_admission(self) -> None:
        """Reopen admission when teardown leaves this exact process alive."""
        if self._terminal_error is None:
            self._dynamic_tool_admission_open = True

    async def wait_dynamic_tool_requests_idle(self) -> None:
        """Wait until the one sequential admitted dynamic request has exited."""
        await self._dynamic_tool_requests_idle.wait()

    async def wait_reader_drained(self) -> None:
        """Wait for buffered stdout and the exit callback without cancelling it."""
        reader = self._reader
        if reader is None:
            return
        if reader is asyncio.current_task():
            raise RuntimeError("codex app-server reader cannot await itself")
        try:
            await asyncio.shield(reader)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            log.warning("codex_app_server: stdout reader stopped by cancellation")
        except Exception as exc:
            log.warning(
                "codex_app_server: stdout reader stopped with error: %s",
                exc,
            )

    async def start(
        self,
        *,
        on_turn_write_observed: Callable[[], None] | None = None,
        on_turn_drain_confirmed: Callable[[], None] | None = None,
        media_projection: Any | None = None, client_message_id: str | None = None,
    ) -> bool:
        if self._proc is not None:
            raise RuntimeError("CodexAppServerSession already started")
        if self._closing:
            raise RuntimeError("Codex app-server terminated before process spawn")
        from infra.db_async import run_in_db
        if not self.control_only:
            from subagents.vendor_write_admission import prepare_session_launch

            await run_in_db(prepare_session_launch, self, 'codex')
        self._proc = await asyncio.create_subprocess_exec(
            *self.build_cmd(),
            cwd=self.working_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT,
            env=await run_in_db(self.build_env),
            start_new_session=os.name == "posix",
        )
        if os.name == "posix":
            self._process_group = CodexProcessGroup(self._proc.pid)
        if self._closing:
            await self.terminate()
            raise RuntimeError("Codex app-server terminated during process spawn")
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        await self._initialize()
        if self.control_only:
            return True
        await self._start_or_resume_thread()
        if self.task is not None:
            return await self.start_turn(
                self.task, history_items=self.history_items, media_projection=media_projection,
                on_write_observed=on_turn_write_observed, on_drain_confirmed=on_turn_drain_confirmed,
                client_message_id=client_message_id,
            )
        return True

    async def _initialize(self) -> None:
        params: dict[str, Any] = {
            "clientInfo": {
                "name": "corsia",
                "title": "CORSIA",
                "version": "0.1.0",
            },
            "capabilities": {
                "experimentalApi": True,
                "dynamicTools": bool(self.dynamic_tools),
            },
        }
        await self._request("initialize", params)
        await self._notify("initialized", {})

    async def list_models(self) -> dict[str, Any]:
        """Collect every page from the app-server ``model/list`` method."""
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await self._request("model/list", params) or {}
            models.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return {"data": models, "nextCursor": None}
            if cursor in seen_cursors:
                raise CodexAppServerError(
                    "codex model/list repeated its pagination cursor",
                    {"cursor": cursor},
                )
            seen_cursors.add(cursor)

    def _dynamic_tool_names(self) -> set[str]:
        """Return the flat set of tool names on this session's tool surface."""
        names: set[str] = set()
        for spec in self.dynamic_tools:
            if not isinstance(spec, dict):
                continue
            fn = spec.get("function")
            source = fn if isinstance(fn, dict) else spec
            name = source.get("name")
            if isinstance(name, str) and name:
                names.add(name)
        return names

    def _is_write_capable(self) -> bool:
        """Whether the model has at least one sanctioned CORSIA write tool."""
        return codex_tools_are_write_capable(self._dynamic_tool_names())

    def _effective_developer_instructions(self) -> str | None:
        """Return developer instructions, reconciling the read-only signal.

        A write-capable session launched under a read-only native sandbox is
        given the write-path clarification so the model uses its CORSIA tools
        instead of treating the whole workspace as read-only.
        """
        return codex_developer_instructions_with_write_path(
            self.developer_instructions,
            write_capable=self._is_write_capable(),
            read_only=codex_sandbox_reads_read_only(self.sandbox_mode),
        )

    def _assert_launch_write_surface(self) -> None:
        """Validate the enforced kind or ordinary task write surface."""
        from subagents.codex_launch_policy import assert_launch_write_surface

        assert_launch_write_surface(self)

    async def _start_or_resume_thread(self) -> None:
        """Start a new provider thread or resume the persisted one."""
        self._assert_launch_write_surface()
        params: dict[str, Any] = {}
        if self.working_dir:
            params["cwd"] = self.working_dir
        if self.model:
            params["model"] = self.model
        # Residual native operations are constrained by both controls. The
        # app-server launch separately disables shell and hosted web search;
        # none of these settings claims to isolate arbitrary configured MCPs.
        params["sandbox"] = self.sandbox_mode
        params["approvalPolicy"] = self.approval_policy
        # Reconcile the read-only native-sandbox signal with the real
        # write-capable CORSIA tool surface so models do not abandon
        # write-bearing tasks (see managed_launch for the full rationale).
        developer_instructions = self._effective_developer_instructions()
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions

        if self.thread_id:
            # Codex restores the thread's persisted dynamic-tool definitions.
            # CORSIA keeps the local registry above so restored calls still
            # have callbacks on this new app-server connection.
            params["threadId"] = self.thread_id
            params["excludeTurns"] = True
            method = "thread/resume"
        else:
            # Dynamic tools register only on thread/start. Flatten the registry's
            # OpenAI-nested shape into Codex DynamicToolSpec objects.
            if self.dynamic_tools:
                params["dynamicTools"] = ev.to_dynamic_tool_specs(self.dynamic_tools)
            params["environments"] = []
            if self.ephemeral is not None:
                params["ephemeral"] = self.ephemeral
            if self.allow_provider_model_fallback is not None:
                params["allowProviderModelFallback"] = (
                    self.allow_provider_model_fallback
                )
            method = "thread/start"

        result = await self._request(method, params)
        thread = (result or {}).get("thread") or {}
        codex_thread_id = thread.get("id")
        if not codex_thread_id:
            raise CodexAppServerError(
                "codex app-server did not return a thread id", result
            )
        self.thread_id = codex_thread_id
        reported_model = (result or {}).get("model")
        if reported_model:
            await self._record_reported_model(str(reported_model))
        started: dict[str, Any] = {
            "kind": "thread_started",
            "thread_id": codex_thread_id,
        }
        # The app-server reports the rollout file backing this thread as
        # ``thread.path`` (absent on servers that predate the field). The
        # file is created lazily, so this is a location to record — never
        # proof that the rollout exists yet.
        rollout_path = thread.get("path")
        if isinstance(rollout_path, str) and rollout_path:
            if self._tool_requests is not None:
                self._tool_requests.rollout_path = rollout_path
                self._tool_requests.fresh_rollout = method == "thread/start"
            started["rollout_path"] = rollout_path
        await self._emit(started)

    async def inject_items(
        self, items: tuple[dict[str, Any], ...] | list[dict[str, Any]]
    ) -> bool:
        if not self.thread_id:
            return False
        if not items:
            return True
        await self._request(
            "thread/inject_items",
            {
                "threadId": self.thread_id,
                "items": list(items),
            },
        )
        return True

    async def start_turn(
        self,
        text: str,
        *,
        history_items: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        model: str | None = None,
        output_schema: dict[str, Any] | None = None,
        media_projection: Any | None = None,
        on_write_observed: Callable[[], None] | None = None,
        on_drain_confirmed: Callable[[], None] | None = None, client_message_id: str | None = None,
    ) -> bool:
        if not self.thread_id:
            return False
        if history_items:
            await self.inject_items(history_items)
        from core.media_delivery import codex_input_items

        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": codex_input_items(text, media_projection),
            "environments": [],
            **({"clientUserMessageId": client_message_id} if client_message_id else {}),
        }
        exact_model = model or self.model
        if exact_model:
            params["model"] = exact_model
            self.model = exact_model
        if output_schema is not None:
            params["outputSchema"] = output_schema
        request_callbacks = {}
        if on_write_observed is not None:
            request_callbacks["on_write_observed"] = on_write_observed
        if on_drain_confirmed is not None:
            request_callbacks["on_drain_confirmed"] = on_drain_confirmed
        if self._tool_requests is not None:
            await self._tool_requests.prepare_turn()
        requested_at = datetime.now(timezone.utc)
        result = await self._request("turn/start", params, **request_callbacks)
        turn_id = ((result or {}).get("turn") or {}).get("id")
        if not turn_id:
            raise CodexAppServerError(
                "codex app-server did not return a turn id", result
            )
        self._track_active_turn(turn_id)
        self._active_turn_started = (turn_id, requested_at)
        return True

    def _track_active_turn(self, turn_id: str) -> asyncio.Future:
        """Own one accepted turn and its terminal-confirmation future.

        Only CORSIA's ``turn/start`` response establishes this identity.
        Interrupt may revisit the same id to establish or reuse its waiter.
        """
        if getattr(self, "_reasoning_turn_id", None) != turn_id:
            self._clear_reasoning_observations(turn_id=turn_id)
        self._active_turn_id = turn_id
        return self._completed_turns.setdefault(
            turn_id, asyncio.get_running_loop().create_future()
        )

    def _clear_reasoning_observations(self, *, turn_id: str | None = None) -> None:
        """Reset parser-only reasoning state at an authoritative Turn boundary."""
        self._reasoning_summary_parts: dict[str, dict[int, str]] = {}
        self._opaque_reasoning_references: set[str] = set()
        self._last_reasoning_summary_item_id: str | None = None
        self._reasoning_turn_id: str | None = turn_id

    def peek_active_usage(self) -> dict | None:
        """Read the active turn's latest occupancy without consuming terminal proof."""
        turn_id = self._active_turn_id
        if not turn_id:
            return None
        return ev.usage_payload(self._usage_by_turn.get(turn_id))

    def pop_active_usage(self) -> dict | None:
        """Consume the active turn's last observation for out-of-band terminal paths."""
        turn_id = self._active_turn_id
        if not turn_id:
            return None
        return ev.usage_payload(self._usage_by_turn.pop(turn_id, None))

    def _observe_turn_volume(self, turn_id: str, token_usage: dict):
        """Feed one cumulative reading to the tracker; return what it newly spent."""
        return self._turn_volume.observe(
            turn_id,
            total=provider_usage.from_codex(token_usage.get("total")),
            last=provider_usage.from_codex(token_usage.get("last")),
        )

    def _is_active_provider_boundary(
        self,
        provider_thread_id: object,
        provider_turn_id: object,
    ) -> bool:
        """Return whether an event belongs to this exact active provider turn."""
        return bool(
            provider_thread_id and provider_thread_id == self.thread_id
            and provider_turn_id and provider_turn_id == self._active_turn_id
        )

    async def steer(
        self,
        text: str,
        *,
        media_projection: Any | None = None,
        on_write_observed: Callable[[], None] | None = None,
        on_drain_confirmed: Callable[[], None] | None = None,
        input_context: tuple[str, int] | None = None, client_message_id: str | None = None,
    ) -> bool:
        client_message_id = client_message_id or (input_context[0] if input_context else None)
        if not self.thread_id or not self._active_turn_id or not self._dynamic_tool_requests_idle.is_set():
            return False
        def observed():
            if input_context is not None:
                self.tool_context.pending_inputs[input_context[0]] = input_context[1]
            if on_write_observed is not None:
                on_write_observed()
        try:
            from core.media_delivery import codex_input_items

            result = await self._request(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "expectedTurnId": self._active_turn_id,
                    "input": codex_input_items(text, media_projection),
                    **({"clientUserMessageId": client_message_id} if client_message_id else {}),
                },
                on_write_observed=observed,
                on_drain_confirmed=on_drain_confirmed,
            )
            return result is not _STEER_NOT_WRITTEN
        except CodexAppServerError as exc:
            if input_context is not None and "code" in exc.payload:
                self.tool_context.pending_inputs.pop(input_context[0], None)
            if exc.is_not_steerable:
                log.warning(
                    "codex_app_server: active turn is not steerable "
                    "expected=%r provider_reported=%r",
                    self._active_turn_id,
                    exc.current_active_turn_id,
                )
                return False
            raise

    async def interrupt(self, timeout: float = 30.0) -> bool:
        """Interrupt the active turn; True only when its terminal is CONFIRMED.

        Returning True on an unconfirmed interrupt let callers start a new
        turn beside a still-streaming one (the 2026-07-09 concurrent-turn
        cascades). On timeout the turn stays active and the caller must
        hard-preempt (kill the process) instead.
        """
        if not self.thread_id:
            return False
        turn_id = self._active_turn_id
        if turn_id is None:
            # No active turn — but a just-completed turn's terminal can still
            # be emitting (the active id clears at the START of the completion
            # handler; waiters resolve at the END). True here must mean "no
            # live turn AND its terminal is fully committed", otherwise the
            # caller hard-preempts a healthy session and writes a bogus
            # interrupted marker beside the real terminal.
            pending = [f for f in self._completed_turns.values() if not f.done()]
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.gather(*pending)),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return False
            return True
        waiter = self._track_active_turn(turn_id)
        await self._request(
            "turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}
        )
        if not waiter.done():
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "codex_app_server: interrupt did not complete in %ss", timeout
                )
                return False
        self._active_turn_id = None
        return True

    def _signal_process(self, *, force: bool) -> None:
        signal_process(self._proc, self._process_group, force=force)

    def _kill_process(self) -> None:
        """Kill the owned family without suppressing the failure exit owner."""
        self._signal_process(force=True)

    async def _wait_process_exit(self, timeout: float) -> None:
        if self._process_group is not None:
            await self._process_group.wait(timeout)
        if self._proc is not None:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)

    async def close(self, timeout: float = 10.0) -> int | None:
        self._closing = True
        if self._proc is None:
            return None
        self._signal_process(force=False)
        try:
            await self._wait_process_exit(timeout)
        except TimeoutError:
            self._kill_process()
            await self._wait_process_exit(timeout)
        if self._reader is not None:
            done, _ = await asyncio.wait({self._reader}, timeout=timeout)
            if not done:
                await self._cancel_reader_bounded(timeout)
        return self._proc.returncode

    async def _cancel_reader_bounded(self, timeout: float) -> None:
        await cancel_reader_bounded(self, timeout, log)

    def _request_terminate(self) -> None:
        self._closing = True
        self._kill_process()

    async def terminate(self, timeout: float = 10.0) -> None:
        """Kill the entire provider family and drain buffered reader callbacks."""
        self._request_terminate()
        if self._proc is not None:
            await self._cancel_reader_bounded(timeout)
            await self._wait_process_exit(timeout)

    async def _request(
        self,
        method: str,
        params: dict | None = None,
        *,
        on_write_observed: Callable[[], None] | None = None,
        on_drain_confirmed: Callable[[], None] | None = None,
    ) -> Any:
        from subagents.codex_requests import request

        return await request(self, method, params, on_write_observed=on_write_observed,
                             on_drain_confirmed=on_drain_confirmed)

    async def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("CodexAppServerSession not started")
        async with self._write_lock:
            payload = {"method": method, "params": params}
            self._proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self._proc.stdin.drain()

    async def _respond(
        self, msg_id: int, *, result: Any = None, error: dict | None = None
    ) -> None:
        await notifications.respond(self, msg_id, result=result, error=error)

    async def _read_loop(self) -> None:
        await read_app_server(self, stdout_limit=STDOUT_LIMIT, log=log)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr.extend(chunk)
        except (OSError, ValueError) as exc:
            log.debug("codex_app_server: stderr read failed: %s", exc)

    async def _handle_message(self, msg: dict) -> None:
        if self._tool_requests is not None:
            if self._tool_requests.dispatch(msg):
                return
        record_frame(getattr(self, "corsia_thread_id", None), "codex", msg)
        if self._on_raw_message is not None:
            await self._on_raw_message(msg)
        if "id" in msg and ("result" in msg or "error" in msg):
            self._handle_response(msg)
            return
        if "id" in msg and "method" in msg:
            await self._handle_server_request(msg)
            return
        method = msg.get("method")
        if method:
            await self._handle_notification(method, msg.get("params") or {})

    def _handle_response(self, msg: dict) -> None:
        request_method = self._pending_methods.pop(msg["id"], None)
        fut = self._pending.pop(msg["id"], None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(CodexAppServerError(str(msg["error"]), msg["error"]))
            return
        result = msg.get("result")
        if request_method == "turn/start" and isinstance(result, dict):
            turn = result.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(turn_id, str) and turn_id:
                self._track_active_turn(turn_id)
        fut.set_result(result)

    async def _handle_server_request(self, msg: dict) -> None:
        msg_id = msg["id"]
        method = msg.get("method")
        raw_params = msg.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        provider_thread_id = params.get("threadId")
        provider_turn_id = params.get("turnId")
        if ev.dynamic_tool_request(method):
            if not self._is_active_provider_boundary(
                provider_thread_id,
                provider_turn_id,
            ):
                log.warning(
                    "codex_app_server: rejected uncorrelated dynamic tool "
                    "request thread=%r expected=%r turn=%r expected_turn=%r",
                    provider_thread_id,
                    self.thread_id,
                    provider_turn_id,
                    self._active_turn_id,
                )
                await self._respond(
                    msg_id,
                    result=_dynamic_tool_rejection(
                        _DYNAMIC_TOOL_IDENTITY_REJECTION,
                    ),
                )
                return
            if not self._dynamic_tool_admission_open:
                await self._respond(
                    msg_id,
                    result=_dynamic_tool_rejection(_DYNAMIC_TOOL_DRAIN_REJECTION),
                )
                return
            self._dynamic_tool_requests_idle.clear()
            try:
                result, error = await self._handle_dynamic_tool_request(params)
                await self._respond(msg_id, result=result, error=error)
            finally:
                if self._tool_requests is None or not self._tool_requests.tasks:
                    self._dynamic_tool_requests_idle.set()
            return
        if method not in _NON_ACTION_SERVER_REQUESTS:
            if self._is_active_provider_boundary(
                provider_thread_id,
                provider_turn_id,
            ):
                await self._emit(
                    ev.provider_tool_violation_event(
                        {
                            "id": msg_id,
                            "type": str(method or "unknown-server-request"),
                        },
                        phase="request",
                        provider_thread_id=provider_thread_id,
                        provider_turn_id=provider_turn_id,
                    )
                )
            else:
                log.warning(
                    "codex_app_server: declined uncorrelated provider-native "
                    "request method=%r thread=%r expected=%r turn=%r "
                    "expected_turn=%r",
                    method,
                    provider_thread_id,
                    self.thread_id,
                    provider_turn_id,
                    self._active_turn_id,
                )
        result, error = ev.approval_response(method)
        await self._respond(msg_id, result=result, error=error)

    async def _handle_dynamic_tool_request(
        self,
        params: dict,
    ) -> tuple[dict | None, dict | None]:
        if self.tool_registry is None:
            return None, {
                "code": -32601,
                "message": "CORSIA dynamic tool registry is unavailable",
            }
        name, arguments, error = _dynamic_tool_values(params)
        if error is not None:
            return None, error
        assert name is not None and arguments is not None
        from subagents.codex_dynamic_tools import execute_dynamic_tool

        return await execute_dynamic_tool(self, name, arguments, params)

    async def _handle_notification(self, method: str, params: dict) -> None:
        if await notifications.handle_thread_notification(self, method, params):
            return
        if method == "item/agentMessage/delta":
            await self._emit({"kind": "text_delta", "text": params.get("delta", "")})
            return
        if await notifications.handle_reasoning_notification(self, method, params):
            return
        if await notifications.handle_item_notification(self, method, params):
            return
        if method == "turn/completed":
            await self._handle_turn_completed(params.get("turn") or {})
            return
        await notifications.handle_error_notification(self, method, params)

    async def _record_reported_model(self, model: str) -> None:
        await self._emit({"kind": "model_reported", "model": model})

    async def _handle_item_started(
        self,
        item: dict,
        *,
        provider_thread_id: str | None = None,
        provider_turn_id: str | None = None,
    ) -> None:
        input_id = item.get("clientId")
        if (item.get("type") == "userMessage" and isinstance(input_id, str)
                and input_id in self.tool_context.pending_inputs
                and self._is_active_provider_boundary(provider_thread_id, provider_turn_id)):
            self.tool_context.consumed_inputs.add(input_id)
        event = ev.item_started_event(
            item,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
        )
        if event is not None and event.get("kind") == "provider_tool_violation":
            await self._emit(event)
            return
        if item.get("type") != "reasoning":
            self._last_reasoning_summary_item_id = None
        if event is not None:
            await self._emit(event)

    async def _handle_item_completed(
        self,
        item: dict,
        *,
        provider_thread_id: str | None = None,
        provider_turn_id: str | None = None,
    ) -> None:
        event = ev.item_completed_event(
            item,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
        )
        if event is not None and event.get("kind") == "provider_tool_violation":
            await self._emit(event)
            return
        item_id = item.get("id")
        summary_parts = (
            self._reasoning_summary_parts.pop(item_id, None)
            if isinstance(item_id, str)
            else None
        )
        opaque_reference_was_streamed = bool(
            isinstance(item_id, str) and item_id in self._opaque_reasoning_references
        )
        if isinstance(item_id, str):
            self._opaque_reasoning_references.discard(item_id)
        if item.get("type") == "reasoning":
            event = ev.item_completed_event(
                item,
                summary_was_streamed=bool(
                    summary_parts and any(summary_parts.values())
                ),
                opaque_reference_was_streamed=opaque_reference_was_streamed,
                provider_thread_id=provider_thread_id,
                provider_turn_id=provider_turn_id,
            )
        if event is not None:
            await self._emit(event)

    async def _handle_turn_completed(self, turn: dict) -> None:
        await notifications.handle_turn_completed(self, turn)

    async def _emit(self, ev: dict) -> None:
        if self._on_event is not None:
            await self._on_event(ev)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None
