"""Read a disposable Claude pipe while guarding its existing loopback tool replies."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from core.checkpoint_cc_context import CCCheckpointContext
from core.checkpoint_types import ProviderCheckpointRejected, WarmForkUnusable
from infra.provider_usage import from_anthropic, unmeasured_ledger_row

_PROCESS_EXIT_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class CCCheckpointOutput:
    stdout: bytes
    stderr: bytes
    truncated: bool
    failure: str | None = None


async def _stop_process(process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), _PROCESS_EXIT_TIMEOUT)
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), _PROCESS_EXIT_TIMEOUT)


class _ToolBoundary:
    """One fork's in-flight tool owner; no queue, retry, or foreground Turn."""

    def __init__(self, process, context, cancel_event, *, on_usage=None) -> None:
        self.process, self.context, self.cancel_event = process, context, cancel_event
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.closed = self.truncated = False
        self.failure: str | None = None
        self.on_usage = on_usage
        self.calls: dict[str, dict] = {}
        self.claimed_tool_calls: set[str] = set()

    async def observe(self, record: dict[str, Any]) -> None:
        # Publish readiness before accounting can await this tool's settlement.
        self.context.observe(record)
        if record.get("type") == "result":
            self.closed = True
        self.changed.set()
        kind, event = record.get("type"), record.get("event") or {}
        message = record.get("message") or {}
        key, raw = None, None
        if kind == "assistant" and message.get("stop_reason") is not None:
            key, raw = message.get("id"), message.get("usage")
        elif kind == "stream_event" and event.get("type") == "message_stop":
            key, raw = self.context.message_id, self.context.usage
        elif kind == "result" and not self.calls:
            key, raw = "aggregate", record.get("usage")
        if key and self.on_usage is not None:
            measured = from_anthropic(raw)
            row = measured.ledger_row() if measured is not None else unmeasured_ledger_row()
            reported = message.get("model")
            model = reported if isinstance(reported, str) and reported.strip() else (self.context.model or "")
            if self.calls.get(key) != row:
                await self.on_usage(str(key), model, row)
                self.calls[key] = row

    async def _ready(self, call_id: str) -> None:
        while not self.context.ready(call_id):
            if self.context.finished and call_id in self.context.tool_ids:
                self.failure = "Claude checkpoint did not report its complete tool-call context"
                self.closed = True
                self.changed.set()
                await _stop_process(self.process)
                raise ProviderCheckpointRejected(self.failure)
            if self.closed or self.process.returncode is not None:
                await _stop_process(self.process)
                raise ProviderCheckpointRejected("checkpoint process has ended")
            self.changed.clear()
            if not self.context.ready(call_id):
                await self.changed.wait()

    async def _cutoff(self) -> None:
        self.truncated = self.closed = True
        self.changed.set()
        await _stop_process(self.process)
        raise ProviderCheckpointRejected("checkpoint reached its context limit")

    async def execute(
        self, operation, call_id: str, name: str, arguments: dict[str, Any],
    ) -> str:
        """Release only the model-declared call once after its response is known."""
        from core._tool_result_delivery_support import ToolResultBindingError
        from tools.catalog import normalize_tool_name

        async with self.lock:
            if self.cancel_event.is_set():
                await _stop_process(self.process)
                raise asyncio.CancelledError
            await self._ready(call_id)
            declared = self.context.tool_calls.get(call_id)
            if (
                declared is None
                or normalize_tool_name(declared[0]) != normalize_tool_name(name)
                or declared[1] != arguments
                or call_id in self.claimed_tool_calls
            ):
                raise ToolResultBindingError("checkpoint provider tool call is undeclared or replayed")
            self.claimed_tool_calls.add(call_id)
            if self.closed or self.process.returncode is not None:
                await _stop_process(self.process)
                raise ProviderCheckpointRejected("checkpoint process has ended")
            if not self.context.fits:
                await self._cutoff()
            try:
                content = await operation()
                fits = self.context.accept_tool_result(content)
            except Exception as error:
                self.failure = f"Claude checkpoint tool failed: {error}"
                self.closed = True
                self.changed.set()
                await _stop_process(self.process)
                raise
            if not fits:
                await self._cutoff()
            return content

    async def close(self) -> None:
        self.closed = True
        self.changed.set()
        if not self.cancel_event.is_set():
            # A completed continuity edit must settle before its snapshot installs.
            async with self.lock:
                pass


async def _read_stdout(process, boundary, captured: bytearray) -> None:
    try:
        while line := await process.stdout.readline():
            captured.extend(line)
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # The result parser retains its established malformed-line policy.
            if isinstance(record, dict):
                await boundary.observe(record)
    finally:
        boundary.closed = True
        boundary.changed.set()


async def _exchange(
    process, request, cancel_event, on_cold_input, boundary, captured, errors, spawned_at,
):
    if request.warm_source is None:
        from core.checkpoint_cc_cold import prepare_cold_payload

        payload = await prepare_cold_payload(
            process, request, cancel_event, on_cold_input, spawned_at,
        )
    else:
        from core.checkpoint_cc_cold import prepare_warm_payload

        payload = await prepare_warm_payload(
            process, request, cancel_event, spawned_at,
        )
        if payload is None:
            raise WarmForkUnusable("Claude checkpoint initial input exceeds selected context window")
    from core.checkpoint_account import require_checkpoint_write

    require_checkpoint_write(request)
    process.stdin.write(payload)
    await process.stdin.drain()
    process.stdin.close()

    async def stderr():
        while chunk := await process.stderr.read(8192):
            errors.extend(chunk)

    readers = [asyncio.create_task(_read_stdout(process, boundary, captured)),
               asyncio.create_task(stderr())]
    try:
        await asyncio.gather(*readers)
        await process.wait()
    finally:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def communicate_checkpoint(
    process, request, cancel_event, on_cold_input=None, *, on_tool_executor=None, spawned_at: float,
) -> CCCheckpointOutput:
    """Capture buffered narrative on cutoff; explicit Stop still cancels the call."""
    context = CCCheckpointContext(request.target, 0)
    boundary = _ToolBoundary(process, context, cancel_event, on_usage=request.on_usage)
    if on_tool_executor is not None:
        try:
            await on_tool_executor(boundary.execute)
        except BaseException:
            await _stop_process(process)
            raise
    captured, errors = bytearray(), bytearray()
    operation = asyncio.create_task(_exchange(
        process, request, cancel_event, on_cold_input, boundary, captured, errors, spawned_at,
    ))
    stopped = asyncio.create_task(cancel_event.wait())
    exited = asyncio.create_task(process.wait())
    try:
        await asyncio.wait({operation, stopped, exited}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            await _stop_process(process)
            raise asyncio.CancelledError
        if exited.done() and not operation.done():
            try:
                await asyncio.wait_for(asyncio.shield(operation), _PROCESS_EXIT_TIMEOUT)
            except TimeoutError:
                boundary.failure = "Claude checkpoint output drain timed out after process exit"
        elif operation.done():
            await operation
    finally:
        try:
            if process.returncode is None:
                await _stop_process(process)
        finally:
            for task in (operation, stopped, exited):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, stopped, exited, return_exceptions=True)
            await boundary.close()
    return CCCheckpointOutput(bytes(captured), bytes(errors), boundary.truncated, boundary.failure)
