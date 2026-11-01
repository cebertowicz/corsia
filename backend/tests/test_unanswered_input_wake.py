"""A Turn that errors after taking in a person's input it never answered queues one wake.

The wake is queued in the terminal write and driven after release. It never
chains, and it is not queued for a limit, sign-in or context-size failure, a
Stop, an answered input, an agent's input, or when plan liveness armed a wake.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from automation.plan_liveness_terminal import on_thread_turn_complete
from automation.turn_end_effects import record_turn_end
from infra.unanswered_input_wake import EVENT, GUIDANCE

_THREAD = "wake-thread"


def _wakes(db) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT id,metadata FROM messages WHERE thread_id=? AND role='system' "
        "AND json_extract(metadata,'$.event_type')=?", (_THREAD, EVENT))]


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
        (_THREAD, "user-1", "oMLX/qwen", "t0", "t0"),
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _take_in(db, turn="t1", message="m1", **metadata) -> None:
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        _THREAD, role="user", content="please answer this", intent="apply_now",
        message_id=message, turn_id=turn, boot_epoch="boot-1", admitted_at="t1",
        metadata=metadata or None, db=db,
    )
    begin_batch_write(admission.batch, attempt_id=f"w-{turn}", started_at="t1", db=db)
    settle_batch_write(f"w-{turn}", outcome="accepted", db=db)
    db.commit()


async def _end(db, turn="t1", *, outcome="error", error="provider exploded", **more):
    from infra.thread_driver import finish_turn

    finish_turn(turn, outcome=outcome, ended_at="t2", successor_turn_id=None,
                boot_epoch="boot-1", error=error, db=db)
    db.commit()
    return await record_turn_end(
        on_thread_turn_complete, _THREAD, outcome=outcome, error=error, turn_id=turn, **more,
    )


@pytest.mark.asyncio
async def test_an_error_after_an_unanswered_input_queues_one_wake(db) -> None:
    _take_in(db)

    effects = await _end(db)

    (wake,) = _wakes(db)
    assert json.loads(wake["metadata"])["failed_turn_id"] == "t1"
    assert effects.drives.count(_THREAD) == 1
    assert GUIDANCE in db.execute("SELECT content FROM messages WHERE id=?", (wake["id"],)).fetchone()[0]


@pytest.mark.asyncio
async def test_a_turn_that_took_in_the_wake_and_errors_queues_no_other(db) -> None:
    """The woken Turn also took in a newer person input, and still queues no chain."""
    _take_in(db)
    await _end(db)
    (wake,) = _wakes(db)
    _take_in(db, turn="t2", message="m2")
    db.execute("UPDATE messages SET active_turn_id='t2',delivery_state='delivered' WHERE id=?", (wake["id"],))
    db.commit()

    effects = await _end(db, turn="t2", error="again")

    assert len(_wakes(db)) == 1
    assert _THREAD not in effects.drives


@pytest.mark.parametrize("kind,error", [
    ("usage_limit", "You've hit your usage limit"),
    ("sign_in_expired", "session expired"),
    ("auth_rejected", "provider rejected the key"),
    ("context_overflow", "context window exceeded"),
])
@pytest.mark.asyncio
async def test_excluded_causes_queue_nothing(db, kind, error) -> None:
    from infra.queries import insert_message

    _take_in(db)
    insert_message(_THREAD, "system", error, metadata={"turn_id": "t1", "cause_kind": kind}, db=db)
    db.commit()

    effects = await _end(db, error=error)

    assert _wakes(db) == []
    assert _THREAD not in effects.drives


@pytest.mark.parametrize("error", [
    "Not logged in · Please run /login",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "You've hit your usage limit",
])
@pytest.mark.asyncio
async def test_a_claude_code_signed_out_or_limited_exit_queues_nothing(db, error) -> None:
    """The exit string alone excludes it; no error row is needed."""
    from automation.turn_end_effects import record_external_turn_end

    _take_in(db)
    from infra.thread_driver import finish_turn

    finish_turn("t1", outcome="error", ended_at="t2", successor_turn_id=None,
                boot_epoch="boot-1", error=error, db=db)
    db.commit()

    effects = await record_external_turn_end(_THREAD, outcome="error", error=error, turn_id="t1")

    assert _wakes(db) == []
    assert _THREAD not in effects.drives


@pytest.mark.asyncio
async def test_a_stop_queues_nothing(db) -> None:
    _take_in(db)

    await _end(db, outcome="cancelled", error=None)

    assert _wakes(db) == []


@pytest.mark.asyncio
async def test_an_answered_input_queues_nothing(db) -> None:
    from infra.queries import insert_message

    _take_in(db)
    insert_message(_THREAD, "assistant", "here is your answer", metadata={"turn_id": "t1"}, db=db)
    db.commit()

    await _end(db)

    assert _wakes(db) == []


@pytest.mark.asyncio
async def test_a_wake_plan_liveness_armed_replaces_ours(db, monkeypatch) -> None:
    from automation import plan_liveness

    monkeypatch.setattr(plan_liveness, "on_turn_complete", lambda *a, **k: SimpleNamespace(provider_reset_at=None))
    _take_in(db)

    effects = await _end(db)

    assert _wakes(db) == []
    assert _THREAD not in effects.drives


@pytest.mark.asyncio
async def test_an_agents_input_is_not_a_persons(db) -> None:
    _take_in(db, sender_kind="agent")

    await _end(db)

    assert _wakes(db) == []


# --- native end to end: a real Turn over a loopback provider, no model process ---

_NATIVE = "native-wake-thread"
_PERSON = "what is the capital of France?"


def _sse(content, finish=None) -> bytes:
    chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": content} if content else {},
                          "finish_reason": finish}]}
    if finish:
        chunk["usage"] = {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}
    return f"data: {json.dumps(chunk)}\n\n".encode()


class _Provider:
    """Refuses with HTTP 400 while ``refusals`` remain, then answers."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.requests: list[dict] = []

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        if not body.get("stream"):
            return web.json_response({"error": {"message": "no titles"}}, status=400)
        self.requests.append(body)
        if self.refusals > 0:
            self.refusals -= 1
            return web.json_response(
                {"error": {"message": "upstream rejected the request", "type": "invalid_request_error"}},
                status=400)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_sse("Paris."))
        await response.write(_sse(None, "stop"))
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def models(self, _request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "m", "object": "model"}]})


def _seed(port: int) -> None:
    from infra.database import write_transaction
    from llm import network_discovery as nd

    sid = nd.upsert_server("external", "wake-inst", "Wake", "127.0.0.1", port, "online", kind="external")
    model = f"external@{sid}/m"
    with write_transaction(None, scope="test_wake_seed") as db:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,"
            "context_window,enabled,available,created_at,updated_at) "
            "VALUES (?,'native','external','m',?,'m',32000,1,1,'t','t')", (model, model))
        db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
                   (_NATIVE, "user-1", model, "t0", "t0"))


def _rows(sql: str) -> list[tuple]:
    from infra.database import get_db

    db = get_db()
    try:
        return [tuple(row) for row in db.execute(sql, (_NATIVE,))]
    finally:
        db.close()


async def _run_native(refusals: int):
    from infra.thread_driver import admit_input
    from infra.thread_execution import drive_claimed_batch

    provider = _Provider(refusals)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", provider.chat)
    app.router.add_get("/v1/models", provider.models)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    try:
        await asyncio.to_thread(_seed, server.port)
        admission = await asyncio.to_thread(
            admit_input, _NATIVE, role="user", content=_PERSON, intent="apply_now",
            message_id="person-1", turn_id="turn-1", boot_epoch="boot-1",
            admitted_at="2026-10-31T00:00:00Z")
        result = await asyncio.wait_for(drive_claimed_batch(admission.batch), timeout=10)
        if getattr(result, "turn_task", None) is not None:
            await asyncio.wait_for(asyncio.gather(result.turn_task, return_exceptions=True), timeout=10)
        for _ in range(200):
            running = _rows("SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND status='running'")[0][0]
            if running == 0:
                await asyncio.sleep(0.3)
                break
            await asyncio.sleep(0.05)
    finally:
        await server.close()
    return provider, [status for (status,) in _rows(
        "SELECT status FROM active_turns WHERE thread_id=? ORDER BY turn_order")]


@pytest.fixture
def no_retry_delay():
    from infra import constants

    delays = list(constants.LLM_RETRY_DELAYS)
    constants.LLM_RETRY_DELAYS[:] = [0] * len(delays)
    yield
    constants.LLM_RETRY_DELAYS[:] = delays


@pytest.mark.asyncio
async def test_a_refused_native_turn_is_driven_and_answered_once(isolated_db, no_retry_delay) -> None:
    provider, statuses = await _run_native(refusals=1)

    assert statuses == ["error", "completed"]
    assert len(_rows("SELECT id FROM messages WHERE thread_id=? AND role='system' AND "
                     f"json_extract(metadata,'$.event_type')='{EVENT}'")) == 1
    assert GUIDANCE in json.dumps(provider.requests[1]["messages"][-1])
    assert _PERSON in json.dumps(provider.requests[1]["messages"])


@pytest.mark.asyncio
async def test_a_woken_native_turn_that_errors_starts_nothing_more(isolated_db, no_retry_delay) -> None:
    provider, statuses = await _run_native(refusals=9)

    assert statuses == ["error", "error"]
    assert len(provider.requests) == 2
