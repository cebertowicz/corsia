"""Count a Claude fork's completed model response and private tool replies."""

from __future__ import annotations

from typing import Any

from core.token_estimation import measure_request
from infra.provider_usage import from_anthropic


class CCForkContext:
    """Use current-call input/output receipts, never cumulative result-envelope usage."""

    def __init__(self, target, input_tokens: int | None) -> None:
        self.target = target
        self.base = input_tokens or 0
        self.input_seeded = input_tokens is not None
        self.output = 0
        self.results = 0
        self.message_id: str | None = None
        self.model: str | None = None
        self.tool_ids: set[str] = set()
        self.tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self.finished = False
        self.input_measured = False
        self.output_measured = False
        self.usage: dict[str, Any] = {}
        self.pending_inputs: dict[str, int] = {}
        self.consumed_inputs: set[str] = set()
        self._covered_inputs: set[str] = set()

    @property
    def tokens(self) -> int:
        return self.base + self.output + self.results + sum(self.pending_inputs.values())

    @property
    def fits(self) -> bool:
        return self.tokens <= self.target.context_window

    def ready(self, call_id: str) -> bool:
        """A tool reply cannot outrun its model response or use another call's receipt."""
        return (self.finished and self.input_measured and self.output_measured
                and call_id in self.tool_ids)

    def _message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            return
        if message_id != self.message_id:
            self.message_id = message_id
            self._covered_inputs = self.consumed_inputs.copy()
            self.results, self.output = 0, 0
            self.model = None
            self.tool_ids.clear()
            self.tool_calls.clear()
            self.usage.clear()
            self.finished = self.input_measured = self.output_measured = False
        model = message.get("model")
        if isinstance(model, str) and model:
            self.model = model
        raw_usage = message.get("usage")
        usage = from_anthropic(raw_usage)
        if (usage is not None and isinstance(raw_usage, dict)
                and type(raw_usage.get("input_tokens")) is int and raw_usage["input_tokens"] >= 0):
            self.base = usage.input_total
            self.input_measured = True
            self.usage = dict(raw_usage)
            for input_id in self._covered_inputs:
                self.pending_inputs.pop(input_id, None)
            self.consumed_inputs.difference_update(self._covered_inputs)
            self._covered_inputs.clear()
        if isinstance(raw_usage, dict):
            self._output(raw_usage, final=message.get("stop_reason") is not None)
        for block in message.get("content") or ():
            self._tool(block)
        if message.get("stop_reason") is not None:
            self.finished = True

    def _output(self, usage: dict[str, Any], *, final: bool = True) -> None:
        count = usage.get("output_tokens")
        if type(count) is int and count >= 0:
            self.output = max(self.output, count)
            self.usage["output_tokens"] = self.output
            self.output_measured = self.output_measured or final

    def final_call_identity(self) -> dict[str, Any]:
        """This call's corrected usage plus the identity needed to receipt it."""
        return {
            "usage": dict(self.usage), "message_id": self.message_id, "model": self.model,
        }

    def _tool(self, block: object) -> None:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            call_id = block.get("id")
            if isinstance(call_id, str) and call_id:
                self.tool_ids.add(call_id)
                name, arguments = block.get("name"), block.get("input")
                if isinstance(name, str) and isinstance(arguments, dict):
                    self.tool_calls[call_id] = (name, arguments)

    def observe(self, record: dict[str, Any]) -> None:
        """Consume the installed CLI's raw assistant/partial-message frames."""
        if (record.get("type") == "user" and isinstance(record.get("uuid"), str)
                and record["uuid"] in self.pending_inputs):
            self.consumed_inputs.add(record["uuid"])
        elif record.get("type") == "assistant":
            self._message(record.get("message") or {})
        elif record.get("type") == "result":
            # Terminal envelopes end waiting, but their aggregate usage cannot
            # substitute for this response's input/output receipt.
            self.finished = True
        elif record.get("type") == "stream_event":
            event = record.get("event") or {}
            kind = event.get("type")
            if kind == "message_start":
                self._message(event.get("message") or {})
            elif kind == "content_block_start":
                self._tool(event.get("content_block"))
            elif kind == "message_delta":
                self._output(event.get("usage") or {})
            elif kind == "message_stop":
                self.finished = True

    def accept_tool_result(self, content: str) -> bool:
        """Count one final projected tool reply, including its image allowance."""
        parts = [{"type": "text", "text": str(content)}]
        if getattr(getattr(content, "media", None), "image", None) is not None:
            parts.append({"type": "image_url"})
        self.results += measure_request({
            "messages": [{"role": "tool", "content": parts}],
        }, target=self.target).tokens
        return self.fits
