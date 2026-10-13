"""Capture Codex checkpoint narrative without mistaking an error for content."""

from __future__ import annotations

import asyncio
from typing import Any

from core.checkpoint_types import (
    CHECKPOINT_CONTEXT_TRUNCATED,
    ProviderCheckpointRejected,
    ProviderCheckpointResult,
)
from infra.provider_usage import TokenUsage, checkpoint_receipt
from subagents.provider_context_limits import is_provider_context_limit


def checkpoint_usage(volume: object) -> dict[str, int | None] | None:
    """Keep canonical disjoint usage rather than re-encoding provider subsets."""
    return checkpoint_receipt(volume) if isinstance(volume, TokenUsage) else None


class CodexCheckpointCapture:
    """The output of one disposable process, including unfinished text deltas."""

    def __init__(self, *, on_usage=None, model="") -> None:
        self.terminal: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.bodies: list[str] = []
        self.partial = ""
        self.usage: dict[str, int | None] | None = None
        self.calls: dict[tuple[str, int], dict[str, Any]] = {}
        self.diagnostic: str | None = None
        self.on_usage, self.model = on_usage, model
        self.published: dict[tuple[str, int], dict[str, Any]] = {}

    async def publish_calls(self) -> None:
        """Finish the existing observation handoff before releasing any tool."""
        if self.on_usage is not None:
            for key, measured in tuple(self.calls.items()):
                if self.published.get(key) != measured:
                    await self.on_usage(f"{key[0]}:{key[1]}", self.model, measured)
                    self.published[key] = dict(measured)

    def record_call(self, turn_id: str, ordinal: int, usage: dict[str, Any]) -> None:
        """Retain one measured call from the session's single usage observer."""
        self.calls[(turn_id, ordinal)] = dict(usage)

    def _failure(self, detail: str, *, truncated: bool) -> None:
        if self.terminal.done():
            return
        if self.bodies or self.partial.strip():
            self.diagnostic = CHECKPOINT_CONTEXT_TRUNCATED if truncated else detail
            self.terminal.set_result(None)
        else:  # No narrative: fail named, never as an empty success.
            self.terminal.set_exception(ProviderCheckpointRejected(
                f"{CHECKPOINT_CONTEXT_TRUNCATED}: {detail}" if truncated else detail))

    def truncate(self) -> None:
        """Finish capture through the existing degraded-completion outcome."""
        self._failure("fork input outgrew its checkpoint window", truncated=True)

    async def on_event(self, event: dict[str, Any]) -> None:
        """Replace streamed copies at item completion; keep earlier fragments."""
        kind, text = event.get("kind"), event.get("text")
        if kind == "assistant_text" and isinstance(text, str) and text.strip():
            self.bodies.append(text)
            self.partial = ""
        elif kind == "text_delta" and isinstance(text, str):
            self.partial += text
        elif kind == "call_usage":
            self.record_call(event["turn_id"], event["ordinal"], event["usage"])
            await self.publish_calls()
        elif kind == "result":
            self.usage = checkpoint_usage(event.get("token_volume")) or self.usage
            if not self.calls and self.on_usage is not None:
                await self.on_usage("aggregate", self.model, self.usage or {})
            if not self.terminal.done():
                self.terminal.set_result(None)
        elif kind == "turn_failed":
            self._failure(
                str(event.get("message") or event.get("cause") or "Codex checkpoint failed"),
                truncated=is_provider_context_limit(event),
            )

    async def on_exit(self, code: int | None, stderr: str | BaseException) -> None:
        """Retain real output on process failure, never its diagnostic as prose."""
        from tools.recorded_call import CompletedToolFailure

        if isinstance(stderr, CompletedToolFailure):
            if not self.terminal.done():
                self.terminal.set_exception(stderr)
            return
        detail = stderr if isinstance(stderr, str) else str(stderr)
        failure = detail.strip() or "Codex checkpoint process exited"
        if code not in {None, 0} and not detail.strip():
            failure = f"Codex checkpoint process exited with code {code}"
        self._failure(failure, truncated=is_provider_context_limit(failure))

    def result(self) -> ProviderCheckpointResult:
        """Normal completion uses its final body; cutoff preserves all partial work."""
        if self.diagnostic == CHECKPOINT_CONTEXT_TRUNCATED:
            body = "\n".join(part for part in (*self.bodies, self.partial) if part.strip())
        else:
            body = self.partial or (self.bodies[-1] if self.bodies else "")
            if not body.strip():
                raise ProviderCheckpointRejected("Codex checkpoint produced no body")
        usage = {"calls": list(self.calls.values())} if self.calls else self.usage
        return ProviderCheckpointResult(body, usage, self.diagnostic)
