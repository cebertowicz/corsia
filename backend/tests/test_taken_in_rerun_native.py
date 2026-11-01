"""A real Native Turn refused by its provider re-runs the person's input once.

Driven end to end over a loopback OpenAI-compatible provider: the first request
gets an HTTP error before any reply, the Turn ends ``error`` through the real
terminal owner, and the one successor's request carries the person's input.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

THREAD = "native-rerun-thread"
PERSON_TEXT = "what is the capital of France?"
BOUND_S = 10.0


def _sse(content: str | None, finish_reason: str | None = None) -> bytes:
    payload = {
        "id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [{"index": 0, "delta": {"content": content} if content else {},
                     "finish_reason": finish_reason}],
    }
    if finish_reason:
        payload["usage"] = {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}
    return f"data: {json.dumps(payload)}\n\n".encode()


class _Provider:
    """Refuses with HTTP 400 while ``refusals`` remain, then answers."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.requests: list[dict] = []

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        if not body.get("stream"):  # the thread-title call, not a Turn
            return web.json_response({"error": {"message": "no titles here"}}, status=400)
        self.requests.append(body)
        if self.refusals > 0:
            self.refusals -= 1
            return web.json_response(
                {"error": {"message": "upstream rejected the request", "type": "invalid_request_error"}},
                status=400,
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_sse("Paris."))
        await response.write(_sse(None, "stop"))
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def models(self, _request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "m", "object": "model"}]})


async def _start(provider: _Provider) -> TestServer:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", provider.chat)
    app.router.add_get("/v1/models", provider.models)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return server


def _port(server: TestServer) -> int:
    assert server.port is not None  # set once the server started
    return server.port


def _seed(port: int) -> str:
    from infra.database import write_transaction
    from llm import network_discovery as nd

    sid = nd.upsert_server("external", "rerun-inst", "Rerun", "127.0.0.1", port, "online", kind="external")
    model = f"external@{sid}/m"
    with write_transaction(None, scope="test_rerun_seed") as db:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,"
            "context_window,enabled,available,created_at,updated_at) "
            "VALUES (?,'native','external','m',?,'m',32000,1,1,'t','t')",
            (model, model),
        )
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
            (THREAD, "user-1", model, "t0", "t0"),
        )
    return model


async def _quiet() -> None:
    """Wait until no Turn on the thread is running and no driver task remains."""
    from infra.database import get_db

    for _ in range(int(BOUND_S / 0.05)):
        db = get_db()
        try:
            running = db.execute(
                "SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND status='running'", (THREAD,),
            ).fetchone()[0]
        finally:
            db.close()
        if running == 0:
            await asyncio.sleep(0.2)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the thread never went idle")


async def _send_and_settle(provider: _Provider) -> list[tuple]:
    from infra.thread_driver import admit_input
    from infra.thread_execution import drive_claimed_batch

    admission = await asyncio.to_thread(
        admit_input, THREAD, role="user", content=PERSON_TEXT, intent="apply_now",
        message_id="person-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="2026-10-31T00:00:00Z",
    )
    assert admission.batch is not None
    result = await asyncio.wait_for(drive_claimed_batch(admission.batch), timeout=BOUND_S)
    if getattr(result, "turn_task", None) is not None:
        await asyncio.wait_for(asyncio.gather(result.turn_task, return_exceptions=True), timeout=BOUND_S)
    await _quiet()
    from infra.database import get_db

    db = get_db()
    try:
        return [tuple(row) for row in db.execute(
            "SELECT id,status FROM active_turns WHERE thread_id=? ORDER BY turn_order", (THREAD,))]
    finally:
        db.close()


def _person_rows() -> list[tuple]:
    from infra.database import get_db

    db = get_db()
    try:
        return [tuple(row) for row in db.execute(
            "SELECT id,content,delivery_state FROM messages WHERE thread_id=? AND role='user'", (THREAD,))]
    finally:
        db.close()


def _error_recovered() -> list[bool]:
    """Each Turn error row's `recovered` mark, oldest first."""
    from infra.database import get_db

    db = get_db()
    try:
        return [json.loads(row[0]).get("recovered") is True for row in db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='system' "
            "AND json_extract(metadata, '$.type')='llm_error' ORDER BY seq", (THREAD,))]
    finally:
        db.close()


def _carries_person_input(request: dict) -> bool:
    return any(PERSON_TEXT in json.dumps(message.get("content")) for message in request["messages"])


@pytest.fixture
def no_retry_delay():
    from infra import constants

    delays = list(constants.LLM_RETRY_DELAYS)
    constants.LLM_RETRY_DELAYS[:] = [0] * len(delays)
    yield
    constants.LLM_RETRY_DELAYS[:] = delays


@pytest.mark.asyncio
async def test_refused_native_turn_reruns_the_person_input_once(isolated_db, no_retry_delay) -> None:
    provider = _Provider(refusals=1)
    server = await _start(provider)
    try:
        await asyncio.to_thread(_seed, _port(server))
        turns = await _send_and_settle(provider)
    finally:
        await server.close()

    assert [status for _id, status in turns] == ["error", "completed"]
    assert _person_rows() == [("person-1", PERSON_TEXT, "delivered")]
    assert len(provider.requests) == 2
    assert all(_carries_person_input(request) for request in provider.requests)
    # The successor asks for the answer through the model-only re-run row.
    assert "Answer that input now." in json.dumps(provider.requests[1]["messages"][-1])
    contents = [json.dumps(message.get("content")) for message in provider.requests[1]["messages"]]
    assert sum(PERSON_TEXT in content for content in contents) == 1  # never re-written
    # The re-run answered it, so the failed Turn's error is past, not a live Retry.
    assert _error_recovered() == [True]


@pytest.mark.asyncio
async def test_a_second_refusal_starts_no_third_run(isolated_db, no_retry_delay) -> None:
    provider = _Provider(refusals=5)
    server = await _start(provider)
    try:
        await asyncio.to_thread(_seed, _port(server))
        turns = await _send_and_settle(provider)
    finally:
        await server.close()

    assert [status for _id, status in turns] == ["error", "error"]
    assert _person_rows() == [("person-1", PERSON_TEXT, "delivered")]
    assert len(provider.requests) == 2
    # Only the error a successor moved past is recovered; the last one stays live.
    assert _error_recovered() == [True, False]
