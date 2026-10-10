"""Local Responses peer for the installed, unmodified Codex CLI."""

import asyncio
import contextlib
import json


def sse(kind, **fields):
    return (f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


class ResponsesPeer:
    """Hold the first response completion independently of the direct callback."""

    def __init__(self, *, code_mode=False, tool_number=1):
        self.code_mode = code_mode
        self.tool_number = tool_number
        self.release = asyncio.Event()
        self.second_request = asyncio.Event()
        self.requests = []
        self.tasks = set()
        self.errors = []

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.splitlines()
                          if line.lower().startswith(b"content-length:"))
            self.requests.append(json.loads(await reader.readexactly(length)))
            number = len(self.requests)
            if number > self.tool_number:
                self.second_request.set()
            items = [{"type": "function_call", "id": "fc-one", "call_id": "direct-one",
                      "name": "continuity_edit", "arguments": "{}"}]
            if self.code_mode:
                items = [{"type": "custom_tool_call", "id": "exec-one", "call_id": "outer-exec",
                          "name": "exec", "input": '// @exec: {"yield_time_ms": 0}\ntext(await tools.continuity_edit({}));'}]
            if number != self.tool_number:
                items = [{"type": "message", "role": "assistant", "id": f"msg-{number}",
                          "content": [{"type": "output_text", "text": "fixture complete"}]}]
            usage = {"input_tokens": 9000, "output_tokens": 700, "total_tokens": 9700,
                     "input_tokens_details": {"cached_tokens": 2000},
                     "output_tokens_details": {"reasoning_tokens": 650}}
            response = {"id": f"response-{number}", "object": "response", "model": "gpt-5.4",
                        "status": "completed", "output": items, "usage": usage}
            prefix = sse("response.created", response={**response, "status": "in_progress", "usage": None})
            for index, item in enumerate(items):
                prefix += sse("response.output_item.added", output_index=index, item=item)
                prefix += sse("response.output_item.done", output_index=index, item=item)
            suffix = sse("response.completed", response=response)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         + f"Content-Length: {len(prefix) + len(suffix)}\r\nConnection: close\r\n\r\n".encode()
                         + prefix)
            await writer.drain()
            if number == self.tool_number:
                await self.release.wait()
            writer.write(suffix)
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception as error:
            self.errors.append(repr(error))
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
