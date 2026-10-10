"""Synthetic Anthropic and loopback peer for the unmodified stock Claude CLI."""

import asyncio
import contextlib
import json


def sse(kind, **fields):
    """Encode one Anthropic streaming event the stock CLI will accept."""
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n".encode()


async def read_request(reader):
    """Read one HTTP request from the CLI; return its method, route and body bytes."""
    head = await reader.readuntil(b"\r\n\r\n")
    method, route, _ = head.splitlines()[0].decode().split()
    length = next((int(line.split(b":", 1)[1]) for line in head.splitlines()
                   if line.lower().startswith(b"content-length:")), 0)
    return method, route, (await reader.readexactly(length) if length else b"")


class ClaudePeer:
    """One fixture-owned port serves upstream SSE and the real MCP HTTP client."""

    def __init__(self, execute, *, input_tokens=100, calls=1):
        self.execute, self.input_tokens, self.calls = execute, input_tokens, calls
        self.release = asyncio.Event()
        self.tool_entered = asyncio.Event()
        self.next_request = asyncio.Event()
        self.requests, self.results, self.errors = [], [], []
        self.tasks = set()

    async def _model_response(self, body, writer):
        self.requests.append(body)
        number = len(self.requests)
        tool = number == 1
        if not tool:
            self.next_request.set()
        prefix = sse("message_start", message={
            "id": f"fixture-{number}", "type": "message", "role": "assistant",
            "content": [], "model": body["model"], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": self.input_tokens if tool else 100,
                "cache_read_input_tokens": 8000 if tool else 0,
                "cache_creation_input_tokens": 1000 if tool else 0, "output_tokens": 1},
        })
        for index in range(self.calls if tool else 1):
            block = ({"type": "tool_use", "id": f"fixture-call-{index}",
                      "name": "mcp__corsia__read_file", "input": {}} if tool else
                     {"type": "text", "text": ""})
            delta = ({"type": "input_json_delta", "partial_json": json.dumps({"path": f"file-{index}"})}
                     if tool else {"type": "text_delta", "text": f"fixture answer {number}"})
            prefix += sse("content_block_start", index=index, content_block=block)
            prefix += sse("content_block_delta", index=index, delta=delta)
            prefix += sse("content_block_stop", index=index)
        tail = sse("message_delta", delta={"stop_reason": "tool_use" if tool else "end_turn",
                   "stop_sequence": None}, usage={"output_tokens": 700}) + sse("message_stop")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     + f"Content-Length: {len(prefix)+len(tail)}\r\nConnection: close\r\n\r\n".encode() + prefix)
        await writer.drain()
        if tool:
            await self.release.wait()
        writer.write(tail)
        await writer.drain()

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            method, route, raw = await read_request(reader)
            body = json.loads(raw) if raw else {}
            status, content_type = "200 OK", "application/json"
            if route == "/tool" and method == "GET":
                data = json.dumps({"tools": [{"type": "function", "function": {
                    "name": "read_file", "description": "Read synthetic fixture",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                }}]}).encode()
            elif route == "/tool":
                self.tool_entered.set()
                try:
                    result = await self.execute(body)
                    data, content_type = str(result).encode(), "text/plain"
                    self.results.append(str(result))
                except Exception as error:
                    self.errors.append(str(error))
                    data, status = str(error).encode(), "500 Internal Server Error"
            elif "/v1/messages/count_tokens" in route:
                data = b'{"input_tokens":1000}'
            elif "/v1/messages" in route:
                await self._model_response(body, writer)
                return
            else:
                data = b"{}"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()+data)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # Expected when the exact failed provider is killed before its reply.
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
