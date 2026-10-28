"""Credential-free real-binary capture at a loopback Responses endpoint."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class CaptureEndpoint:
    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                # Never retain headers: even this test must not capture auth.
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                event = {"type": "response.completed", "response": {
                    "id": "resp_confined_capture", "object": "response", "status": "completed",
                    "output": [], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(("event: response.completed\ndata: " + json.dumps(event) + "\n\n").encode())

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def config(self):
        return (
            'model_provider="stub"',
            'model_providers.stub={name="stub",base_url="http://127.0.0.1:'
            f'{self.server.server_port}/v1",wire_api="responses",requires_openai_auth=false}}',
        )


class AppServerClient:
    def __init__(self, session):
        self.session = session
        self.counter = 0
        self.process = None
        self.stderr = None

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.session.build_cmd(), cwd=self.session.working_dir,
            env=self.session.build_env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        # Drain stderr to avoid deadlock, but never emit raw logs.
        self.stderr = asyncio.create_task(self.process.stderr.read())
        await self.rpc("initialize", {
            "clientInfo": {"name": "confined_capture", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await self.send({"method": "initialized"})
        return self

    async def __aexit__(self, *_args):
        if self.process.returncode is None:
            self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 10)
        await self.stderr

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 20)
        if not line:
            # This fixture has synthetic auth only and connects to loopback.
            diagnostic = (await self.stderr).decode(errors="replace")
            raise AssertionError("Codex exited before capture: " + diagnostic[:2000])
        return json.loads(line)

    async def rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.read()
            if message.get("id") == request_id:
                assert "error" not in message, message.get("error")
                return message.get("result")

    async def complete_turn(self, thread_id):
        await self.rpc("turn/start", {"threadId": thread_id, "input": [{
            "type": "text", "text": "Run a command, read outside the grant, and search the web.",
        }]})
        while True:
            message = await self.read()
            if message.get("method") == "turn/completed":
                assert message["params"]["turn"]["status"] == "completed"
                return
