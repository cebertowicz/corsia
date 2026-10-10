"""Managed tool receipt waits while the stock Codex pipe keeps reading."""

from __future__ import annotations

import asyncio
import logging

from core._tool_result_delivery_support import ToolResultBindingError
from subagents.codex_app_server_events import dynamic_tool_request
from subagents.codex_dynamic_tools import (
    checkpoint_reply_fits,
    dynamic_tool_response,
    dynamic_tool_values,
)
from subagents.codex_durable_receipts import ReceiptArtifactError, RolloutReceiptCursor
from tools.recorded_call import CompletedToolFailure

log = logging.getLogger(__name__)

# A callback without its exact receipt cannot execute. Bound this prerequisite
# wait, not ordinary generation or the tool's own execution time.
TOOL_RECEIPT_TIMEOUT_SECONDS = 30.0
_DYNAMIC_TOOL_BINDING_REJECTION = (
    "Error: Codex dynamic tool request could not be bound to one live provider call"
)


class ToolRequests:
    """Own managed callbacks and their exact current-response receipt cursor."""

    @classmethod
    def for_session(cls, session) -> ToolRequests | None:
        """Attach control queries only to checkpoint or ordinary managed tools."""
        if session.checkpoint_tool_result_fits or (
            session.corsia_thread_id and session.tool_registry is not None
        ):
            return cls(session)
        return None

    def __init__(self, session) -> None:
        self.session = session
        self.tasks: set[asyncio.Task] = set()
        self.seen: set[int | str] = set()
        self.turn_id: str | None = None
        self.lock = asyncio.Lock()
        self.cursor: RolloutReceiptCursor | None = None
        self.rollout_path: str | None = None
        self.fresh_rollout = False

    async def prepare_turn(self) -> None:
        """Freeze the exact rollout cursor before the turn/start pipe write."""
        async with self.lock:
            path = self.rollout_path
            if not path:
                raise ReceiptArtifactError("managed Codex thread returned no rollout path")
            self.cursor = await asyncio.to_thread(
                RolloutReceiptCursor, path, self.session.thread_id,
                fresh=self.fresh_rollout,
            )
            self.fresh_rollout = False
            self.seen.clear()
            self.turn_id = None

    def dispatch(self, frame: dict) -> bool:
        """Schedule only correlated managed tools; replayed RPC IDs run once."""
        if not dynamic_tool_request(frame.get("method")) or "id" not in frame:
            return False
        params = frame.get("params") or {}
        if not self.session._is_active_provider_boundary(params.get("threadId"), params.get("turnId")):
            return False
        if params.get("turnId") != self.turn_id:
            self.seen.clear()
            self.turn_id = params["turnId"]
        if frame["id"] in self.seen:
            return True
        if not self.session._dynamic_tool_admission_open:
            return False  # Existing handler owns the immediate draining rejection.
        self.seen.add(frame["id"])
        self.session._dynamic_tool_requests_idle.clear()
        # Freeze reader order: this request follows its own item/started
        # declaration, which a bound live owner may defer behind input settlement.
        delivery = getattr(self.session, "received_event_delivery", None)
        declared = delivery() if delivery is not None else None
        task = asyncio.create_task(self._execute(frame, declared))
        self.tasks.add(task)
        return True

    def measure_tool_reply(self, reply: dict, target) -> int:
        """Count current output and all final replies, without recording spend."""
        usage = self.session._usage_by_turn.get(self.session.active_turn_id)
        if not isinstance(usage, dict):
            raise ValueError("Codex tool reply lacks its current usage receipt")
        context = self.session.tool_context
        context.target = target
        context.record_receipt(usage)
        context.accept_tool_result(reply)
        return context.tokens

    async def _usage(self, frame: dict) -> dict:
        if self.cursor is None:
            raise ReceiptArtifactError("managed Codex Turn has no pre-Turn rollout cursor")
        params = frame["params"]
        call_id = params.get("callId") or params.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ReceiptArtifactError("managed Codex RPC has no direct call ID")
        try:
            async with asyncio.timeout(TOOL_RECEIPT_TIMEOUT_SECONDS):
                while self.session._is_active_provider_boundary(params.get("threadId"), params.get("turnId")):
                    usage = await asyncio.to_thread(
                        self.cursor.read_receipt,
                        params["turnId"],
                        call_id,
                    )
                    if usage is not None:
                        return usage
                    await asyncio.sleep(0.05)
        except TimeoutError as error:
            raise ReceiptArtifactError(
                "Timed out waiting for the current Codex tool usage receipt "
                f"after {TOOL_RECEIPT_TIMEOUT_SECONDS:g}s; "
                "the callback could not be matched to its provider thread, turn, "
                "and call ID. The tool was not executed."
            ) from error
        raise asyncio.CancelledError

    async def _reject(self, frame: dict, message: str) -> None:
        await self.session._respond(
            frame["id"],
            result=dynamic_tool_response(message),
        )

    async def _execute(self, frame: dict, declared: asyncio.Task[None] | None = None) -> None:
        try:
            async with self.lock:
                if declared is not None:
                    # Input settlement can hold the declaration's Assistant Step;
                    # a failed, dropped, or overdue declaration rejects below.
                    await asyncio.wait(
                        (declared,), timeout=TOOL_RECEIPT_TIMEOUT_SECONDS,
                    )
                if (
                    self.session._closing
                    or self.session._terminal_error is not None
                ):
                    return
                if not self.session._dynamic_tool_admission_open:
                    # A temporary drain can leave this process alive. Its queued
                    # requests still need the existing explicit rejection.
                    await self.session._handle_server_request(frame)
                    return
                _name, _arguments, error = dynamic_tool_values(frame["params"])
                if error is not None:
                    await self.session._handle_server_request(frame)
                    return
                usage = await self._usage(frame)
                from subagents.codex_app_server_notifications import record_usage

                await record_usage(self.session, frame["params"]["turnId"], usage)
                boundary = self.session.checkpoint_tool_result_fits
                if boundary is not None and not await checkpoint_reply_fits(self.session, None, usage):
                    self.session._request_terminate()
                    return
                if boundary is None:
                    from subagents.codex_app_server_events import usage_payload

                    if usage_payload(usage) is None:
                        raise ValueError("Codex tool-boundary receipt lacks input usage")
                await self.session._handle_server_request(frame)
        except ToolResultBindingError:
            # A bad live binding must answer Codex rather than leaving its RPC open.
            log.exception("Codex tool result could not be bound; rejecting the callback")
            await self._reject(frame, _DYNAMIC_TOOL_BINDING_REJECTION)
        except CompletedToolFailure as error:
            # The transport-exit owner must receive the retained bytes, not a
            # stringified task error that would make the completed effect vanish.
            self.session.revoke_dynamic_tool_admission()
            self.session.retain_completed_tool_failure(error)
            log.exception("Codex completed tool result could not be published")
            self.session._kill_process()
        except Exception as error:
            # A missing measurement never authorizes releasing the tool response.
            self.session.retain_control_failure(error)
            self.session.revoke_dynamic_tool_admission()
            message = f"Tool usage/control failed: {type(error).__name__}: {error}"
            log.exception(message)
            self.session._stderr.extend(message.encode("utf-8"))
            # The existing reader drain and on_exit own failure settlement.
            # Intentional-close would suppress that owner; no synthetic terminal.
            self.session._kill_process()
        finally:
            self.tasks.discard(asyncio.current_task())
            if not self.tasks:
                self.session._dynamic_tool_requests_idle.set()

    async def close(self) -> None:
        """Drain this session's callbacks before its owner can finish teardown."""
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
