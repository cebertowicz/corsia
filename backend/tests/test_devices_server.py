"""The real Devices server over loopback TLS: protocol choice, and what an explicit stop ends."""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time

import h2.config
import h2.connection
import h2.events
import h2.settings
import httpx
import pytest
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.responses import StreamingResponse
from starlette.routing import Route
from wsproto.connection import Connection, ConnectionType
from wsproto.events import CloseConnection, Message, TextMessage

from infra import devices_listener, devices_server
from infra.devices_identity import ensure_host_key
from infra.devices_listener import DevicesListener


class _StubbornStream:
    """An event stream that never reads its receive channel, so it never learns its client went away."""

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: ready\n\n", "more_body": True})
        await asyncio.Event().wait()


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/events")
    async def events():
        async def stream():
            yield b"data: ready\n\n"
            await asyncio.Event().wait()
        return StreamingResponse(stream(), media_type="text/event-stream")

    app.router.routes.append(Route("/stubborn", _StubbornStream()))

    @app.websocket("/ws")
    async def echo(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ready")
        try:
            while True:
                await ws.send_text("echo:" + await ws.receive_text())
        except WebSocketDisconnect:
            pass

    return app


@pytest.fixture
def listener(monkeypatch, tmp_path):
    def prebind():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(socket.SOMAXCONN)
        sock.setblocking(False)
        return sock, sock.getsockname()[1], None

    monkeypatch.setattr(devices_listener, "_prebind", prebind)
    monkeypatch.setattr(devices_listener, "ensure_host_key", lambda: ensure_host_key(tmp_path))
    monkeypatch.setattr(devices_listener.DNSServiceAnnouncement, "start", lambda self: True)
    monkeypatch.setattr(devices_listener.DNSServiceAnnouncement, "stop", lambda self: None)
    return DevicesListener(app=_app())


async def _started(listener: DevicesListener) -> str:
    assert await listener.start() is True
    assert listener.server is not None
    while not listener.server.started:
        await asyncio.sleep(0.001)
    return f"https://127.0.0.1:{listener.port}"


def _tls(alpn: list[str] | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if alpn:
        context.set_alpn_protocols(alpn)
    return context


class _H2WebSocket:
    """A WebSocket over HTTP/2 extended CONNECT (RFC 8441), the way a browser opens one on an h2 connection."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.h2 = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
        self.ws = Connection(ConnectionType.CLIENT)
        self.stream_id = 1
        self.connect_protocol = False
        self.status: bytes | None = None
        self.messages: list[str] = []
        self.close_code: int | None = None

    @classmethod
    async def open(cls, port: int, path: str) -> _H2WebSocket:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_tls(["h2"]))
        assert writer.get_extra_info("ssl_object").selected_alpn_protocol() == "h2"
        socket_ = cls(reader, writer)
        socket_.h2.initiate_connection()
        await socket_._flush()
        while not socket_.connect_protocol:
            await socket_._read()
        socket_.h2.send_headers(socket_.stream_id, [
            (":method", "CONNECT"), (":protocol", "websocket"), (":scheme", "https"),
            (":path", path), (":authority", f"127.0.0.1:{port}"), ("sec-websocket-version", "13"),
        ])
        await socket_._flush()
        while socket_.status is None:
            await socket_._read()
        return socket_

    async def _flush(self):
        self.writer.write(self.h2.data_to_send())
        await self.writer.drain()

    async def _read(self) -> bool:
        data = await self.reader.read(65536)
        if not data:
            return False
        for event in self.h2.receive_data(data):
            if isinstance(event, h2.events.RemoteSettingsChanged):
                setting = event.changed_settings.get(h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL)
                self.connect_protocol = self.connect_protocol or (setting is not None and setting.new_value == 1)
            elif isinstance(event, h2.events.ResponseReceived):
                self.status = dict(event.headers)[b":status"]
            elif isinstance(event, h2.events.DataReceived):
                self.h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                self.ws.receive_data(event.data)
                for ws_event in self.ws.events():
                    if isinstance(ws_event, TextMessage):
                        self.messages.append(ws_event.data)
                    elif isinstance(ws_event, CloseConnection):
                        self.close_code = ws_event.code
        await self._flush()
        return True

    async def send_text(self, text: str):
        self.h2.send_data(self.stream_id, self.ws.send(Message(data=text)))
        await self._flush()

    async def until(self, done) -> None:
        while not done():
            if not await self._read():
                return

    def close(self):
        self.writer.transport.abort()


def _silent_tls_peer(port: int) -> ssl.SSLSocket:
    """A peer that starts an event stream and then never reads again, so it never answers a close_notify."""
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    peer = _tls(["http/1.1"]).wrap_socket(raw)
    peer.sendall(b"GET /events HTTP/1.1\r\nhost: devices\r\n\r\n")
    return peer


async def _until_ended(lines) -> None:
    """Read an event stream until the server ends it, by closing it or by dropping the connection."""
    try:
        async for _line in lines:
            pass
    except httpx.HTTPError:
        pass


async def test_alpn_negotiates_h2_and_still_serves_an_http11_only_client(listener):
    base = await _started(listener)
    try:
        async with httpx.AsyncClient(http2=True, verify=_tls(), trust_env=False) as h2_client:
            response = await h2_client.get(f"{base}/ping")
        async with httpx.AsyncClient(http2=False, verify=_tls(), trust_env=False) as h11_client:
            fallback = await h11_client.get(f"{base}/ping")
    finally:
        await listener.stop()

    assert (response.http_version, response.json()) == ("HTTP/2", {"ok": True})
    assert (fallback.http_version, fallback.json()) == ("HTTP/1.1", {"ok": True})


async def test_a_websocket_works_over_http2_extended_connect_and_http11_upgrade(listener):
    await _started(listener)
    try:
        over_h2 = await _H2WebSocket.open(listener.port, "/ws")
        assert over_h2.status == b"200"
        await over_h2.until(lambda: over_h2.messages)
        await over_h2.send_text("hi")
        await over_h2.until(lambda: len(over_h2.messages) == 2)
        over_h2.close()
        async with websockets.connect(f"wss://127.0.0.1:{listener.port}/ws", ssl=_tls()) as over_h11:
            assert await over_h11.recv() == "ready"
            await over_h11.send("hi")
            assert await over_h11.recv() == "echo:hi"
    finally:
        await listener.stop()

    assert over_h2.messages == ["ready", "echo:hi"]


async def test_an_explicit_stop_ends_open_streams_and_websockets_promptly_and_a_later_start_serves(
    listener, monkeypatch,
):
    """SSE on h2 (one that never reads http.disconnect) and on http/1.1, a WebSocket on each, and a TLS peer
    that never reads or answers a close_notify."""
    # The last-resort cancel would hide a stop that waits on a peer or on a stream; take it out of reach.
    monkeypatch.setattr(devices_server, "_CONNECTION_END_SECONDS", 30)
    base = await _started(listener)
    port = listener.port
    h2_client = httpx.AsyncClient(http2=True, verify=_tls(), trust_env=False, timeout=5)
    h11_client = httpx.AsyncClient(http2=False, verify=_tls(), trust_env=False, timeout=5)
    silent = await asyncio.get_running_loop().run_in_executor(None, _silent_tls_peer, port)
    try:
        async with h2_client.stream("GET", f"{base}/stubborn") as sse_h2, \
                h11_client.stream("GET", f"{base}/events") as sse_h11:
            assert (sse_h2.http_version, sse_h11.http_version) == ("HTTP/2", "HTTP/1.1")
            h2_lines, h11_lines = sse_h2.aiter_lines(), sse_h11.aiter_lines()
            assert await anext(h2_lines) == "data: ready"
            assert await anext(h11_lines) == "data: ready"
            ws_h2 = await _H2WebSocket.open(port, "/ws")
            await ws_h2.until(lambda: ws_h2.messages)
            async with websockets.connect(f"wss://127.0.0.1:{port}/ws", ssl=_tls()) as ws_h11:
                assert await ws_h11.recv() == "ready"
                h2_closing = asyncio.create_task(ws_h2.until(lambda: ws_h2.close_code is not None))

                started = time.monotonic()
                await listener.stop()
                elapsed = time.monotonic() - started

                await asyncio.wait_for(ws_h11.wait_closed(), timeout=2)
                await asyncio.wait_for(h2_closing, timeout=2)
                await asyncio.wait_for(_until_ended(h2_lines), timeout=2)
                await asyncio.wait_for(_until_ended(h11_lines), timeout=2)
    finally:
        await h2_client.aclose()
        await h11_client.aclose()
        silent.close()

    assert elapsed < 2, elapsed
    assert ws_h11.close_code == 1012
    assert ws_h2.close_code == 1012
    assert listener.server is None and listener.task is None and listener.port is None
    # The listener's own connection tasks and ASGI requests were joined, not left behind.
    leftover = [
        task for task in asyncio.all_tasks()
        if any(name in repr(task.get_coro()) for name in ("serve.<locals>.connected", "task_group._handle"))
    ]
    assert leftover == []

    base = await _started(listener)
    try:
        async with httpx.AsyncClient(http2=True, verify=_tls(), trust_env=False) as client:
            again = await client.get(f"{base}/ping")
    finally:
        await listener.stop()
    assert again.status_code == 200


async def test_requests_are_access_logged_like_the_main_servers_with_protocol_and_no_query(listener, caplog):
    from infra.access_log import install_access_log_redaction

    install_access_log_redaction()
    base = await _started(listener)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            async with httpx.AsyncClient(http2=True, verify=_tls(), trust_env=False) as client:
                await client.get(f"{base}/ping", params={"token": "secret-session"})
            async with httpx.AsyncClient(http2=False, verify=_tls(), trust_env=False) as client:
                await client.get(f"{base}/ping")
    finally:
        await listener.stop()

    lines = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    assert len(lines) == 2 and all(line.startswith("127.0.0.1:") for line in lines), lines
    assert lines[0].endswith('"GET /ping HTTP/2" 200')
    assert lines[1].endswith('"GET /ping HTTP/1.1" 200')
    assert "secret-session" not in caplog.text


async def test_a_long_lived_http2_connection_is_not_closed_for_its_request_count(listener, caplog):
    base = await _started(listener)
    try:
        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            async with httpx.AsyncClient(http2=True, verify=_tls(), trust_env=False) as client:
                for _ in range(1100):
                    assert (await client.get(f"{base}/ping")).status_code == 200
    finally:
        await listener.stop()

    # Each access line names the peer's address and port: one port means one connection carried every request.
    peers = {record.getMessage().split(" - ", 1)[0] for record in caplog.records if record.name == "uvicorn.access"}
    assert len(peers) == 1, peers


async def test_a_requested_stop_is_not_a_serve_failure_but_a_bad_key_is(listener, monkeypatch, tmp_path, caplog):
    await _started(listener)
    await listener.stop()
    assert listener.listening == {"error": None}

    key, cert = tmp_path / "bad.key", tmp_path / "bad.crt"
    key.write_text("not a key")
    cert.write_text("not a certificate")
    monkeypatch.setattr(devices_listener, "ensure_host_key", lambda: (key, cert))
    assert await listener.start() is True
    for _ in range(100):
        if listener.reason == "serve":
            break
        await asyncio.sleep(0.01)
    assert listener.listening == {"error": "Devices listener stopped serving", "reason": "serve"}
