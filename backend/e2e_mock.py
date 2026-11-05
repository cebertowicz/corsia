"""Boot-time E2E mock mode with deterministic LLM responses."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi import APIRouter, HTTPException
from loguru import logger

import e2e_mock_calls as mock_calls
from e2e_mock_harnesses import connect_fixture_harnesses
import e2e_pressure_fixture as pressure_fixture
from e2e_mock_responses import _make_chunk, _make_non_streaming, _stream_tool_calls

_mock_replies: list[dict] = []
_mock_gates: dict[str, asyncio.Event] = {}
_call_count: int = 0
_consumed_targeted_replies: set[int] = set()
_last_targeted_replies: dict[bool, dict] = {}
_last_thread_replies: dict[str, dict] = {}

_DEFAULT_REPLY: dict = {
    "role": "assistant",
    "content": "This is a test response.",
    "finish_reason": "stop",
}


def _reset_mock_state(replies: list[dict] | None = None) -> None:
    """Replace the mock response queue and its response and completion gates."""
    global _mock_replies, _mock_gates, _call_count
    global _consumed_targeted_replies, _last_targeted_replies, _last_thread_replies
    for event in _mock_gates.values():
        event.set()
    _mock_replies = replies or []
    _mock_gates = {
        gate_name: asyncio.Event()
        for reply in _mock_replies for key in ("response_gate", "completion_gate")
        if isinstance((gate_name := reply.get(key)), str) and gate_name
    }
    _call_count = 0
    _consumed_targeted_replies = set()
    _last_targeted_replies = {}
    _last_thread_replies = {}
    mock_calls.reset()


def _thread_reply(thread_id: str) -> dict:
    """Serve the calling thread's own seeded replies in order, then repeat its last."""
    for index, reply in enumerate(_mock_replies):
        if index in _consumed_targeted_replies or reply.get("match_thread") != thread_id:
            continue
        _consumed_targeted_replies.add(index)
        _last_thread_replies[thread_id] = reply
        return reply
    return _last_thread_replies.get(thread_id, _DEFAULT_REPLY.copy())


def _next_reply(*, stream: bool | None = None) -> dict:
    """Return the reply for this call shape, preserving legacy FIFO by default.

    A queue whose replies carry ``match_thread`` serves only those threads: a call
    from any other thread gets the default reply and is not counted, but it is
    logged at WARNING and listed by ``GET /api/test/mock-responses``.
    """
    global _call_count
    targeted_threads = {reply["match_thread"] for reply in _mock_replies if "match_thread" in reply}
    if targeted_threads:
        thread_id = mock_calls.calling_thread_id()
        if thread_id is None or thread_id not in targeted_threads:
            mock_calls.record(thread_id, stream)
            return _DEFAULT_REPLY.copy()
        _call_count += 1
        return _thread_reply(thread_id)

    call_index = _call_count
    _call_count += 1
    if not _mock_replies:
        return _DEFAULT_REPLY.copy()

    targeted = stream is not None and any(
        "match_stream" in reply for reply in _mock_replies
    )
    if targeted:
        assert stream is not None
        stream_key: bool = stream
        for index, reply in enumerate(_mock_replies):
            if index in _consumed_targeted_replies:
                continue
            if reply.get("match_stream") is stream_key:
                _consumed_targeted_replies.add(index)
                _last_targeted_replies[stream_key] = reply
                return reply
        for index, reply in enumerate(_mock_replies):
            if index in _consumed_targeted_replies or "match_stream" in reply:
                continue
            _consumed_targeted_replies.add(index)
            _last_targeted_replies[stream_key] = reply
            return reply
        return _last_targeted_replies.get(stream_key, _DEFAULT_REPLY.copy())

    idx = min(call_index, len(_mock_replies) - 1)
    return _mock_replies[idx]


def _release_mock_gate(gate_name: str) -> bool:
    event = _mock_gates.get(gate_name)
    if event is None:
        return False
    event.set()
    return True


async def _wait_for_gate(reply: dict, key: str) -> None:
    gate_name = reply.get(key)
    if gate_name is None:
        return
    if not isinstance(gate_name, str) or not gate_name:
        raise ValueError(f"{key} must be a non-empty string")
    event = _mock_gates.get(gate_name)
    if event is None:
        raise RuntimeError(f"Mock {key} {gate_name!r} was not seeded")
    await event.wait()


async def _streaming_iter(reply: dict):
    """Yield litellm-style streaming chunks with optional delayed completion."""
    delay_ms = reply.get("delay_ms", 0) or 0
    reasoning = reply.get("reasoning_content")

    if delay_ms > 0 and not reasoning:
        await asyncio.sleep(delay_ms / 1000.0)

    if reasoning:
        mid = len(reasoning) // 2
        yield _make_chunk(reasoning_content=reasoning[:mid] or reasoning)
        if mid > 0:
            yield _make_chunk(reasoning_content=reasoning[mid:])
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    await _wait_for_gate(reply, "completion_gate")

    content = reply.get("content")
    if content:
        mid = len(content) // 2
        yield _make_chunk(content=content[:mid] or content)
        if mid > 0:
            yield _make_chunk(content=content[mid:])

    async for chunk in _stream_tool_calls(reply):
        yield chunk

    yield _make_chunk(finish_reason=reply.get("finish_reason", "stop"))


async def _mock_acompletion(**kwargs: Any) -> Any:
    """Drop-in replacement for ``llm.base.acompletion``."""
    reply = _next_reply(stream=bool(kwargs.get("stream")))
    await _wait_for_gate(reply, "response_gate")
    if kwargs.get("stream"):
        return _streaming_iter(reply)
    delay_ms = reply.get("delay_ms", 0) or 0
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)
    await _wait_for_gate(reply, "completion_gate")
    return _make_non_streaming(reply)


async def _skip_harness_discovery() -> list:
    """Keep mock E2E startup from probing ambient subscription CLIs."""
    return []


@contextmanager
def mock_engine():
    """Patch completion and model availability within the E2E lifespan."""
    from llm import model_status

    production_active_providers = model_status._cloud_providers_active
    with (
        patch("llm.base.acompletion", new=_mock_acompletion),
        patch("app_lifecycle._startup_harness_discovery", new=_skip_harness_discovery),
        patch.object(
            model_status,
            "_cloud_providers_active",
            new=lambda: production_active_providers() | {"mock"},
        ),
    ):
        logger.warning(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  ⚠️  MOCK MODE ACTIVE                                      ║\n"
            "║  All LLM responses are seeded mocks.                       ║\n"
            "║  DB = ephemeral E2E mock DB  |  uploads = uploads-e2e-mock ║\n"
            "╚══════════════════════════════════════════════════════════════╝"
        )
        yield


def seed_mock_catalog() -> None:
    """Insert a synthetic model row for mock-mode UI tests."""
    from infra.database import get_db
    from infra import state as _state

    db = get_db()
    now = "2026-01-01T00:00:00Z"
    db.execute(
        """INSERT OR REPLACE INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            backend, source, enabled, available, context_window,
            supports_vision, supports_tools, supports_reasoning,
            default_temperature, default_max_tokens,
            discovered_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,1,1,128000,1,1,1,NULL,8192,?,?,?,?)""",
        ("mock-test-model", "mock", "test-model", "mock/test-model",
         "Mock Test Model", "api", "mock", now, now, now, now),
    )
    db.execute(
        """INSERT OR REPLACE INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            backend, source, enabled, available, context_window,
            supports_vision, supports_tools, supports_reasoning,
            default_temperature, default_max_tokens,
            discovered_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,1,1,?,1,1,1,NULL,8192,?,?,?,?)""",
        (pressure_fixture.PRESSURE_MODEL_BY_HARNESS["native"], "mock", "pressure-model", "mock/pressure-model",
         "Mock Pressure Model", "api", "mock", pressure_fixture.PRESSURE_CONTEXT_WINDOW,
        now, now, now, now),
    )
    fixture_binaries = {
        harness: os.environ.get(env_name)
        for harness, env_name in (
            ("claude-code", "CORSIA_E2E_CC_BINARY"),
            ("codex", "CORSIA_E2E_CODEX_BINARY"),
        )
    }
    if any(fixture_binaries.values()) and not all(fixture_binaries.values()):
        raise RuntimeError("matrix fixture binaries must be configured together")
    harness_rows = (
        (
            "claude-code/fixture-cc", "claude-code", "fixture-cc",
            "claude-code/fixture-cc", "Mock Claude Code", "cc", 393_216,
        ),
        (
            pressure_fixture.PRESSURE_MODEL_BY_HARNESS["claude-code"], "claude-code",
            "fixture-cc-pressure", pressure_fixture.PRESSURE_MODEL_BY_HARNESS["claude-code"],
            "Mock Claude Code Pressure", "cc", pressure_fixture.PRESSURE_CONTEXT_WINDOW,
        ),
        (
            "codex/fixture-codex", "codex", "fixture-codex",
            "codex/fixture-codex", "Mock Codex", "codex", 393_216,
        ),
        (
            pressure_fixture.PRESSURE_MODEL_BY_HARNESS["codex"], "codex",
            "fixture-codex-pressure", pressure_fixture.PRESSURE_MODEL_BY_HARNESS["codex"],
            "Mock Codex Pressure", "codex", pressure_fixture.PRESSURE_CONTEXT_WINDOW,
        ),
    )
    if all(fixture_binaries.values()):
        db.executemany(
            """INSERT OR REPLACE INTO models
               (id,harness,provider,provider_model_id,litellm_model,display_name,
                backend,source,enabled,available,context_window,supports_vision,
                supports_tools,supports_reasoning,default_temperature,
                default_max_tokens,metadata_json,discovered_at,last_seen_at,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'mock',1,1,?,1,1,1,NULL,8192,?,?,?,?,?)""",
            (
                (
                    model_id, harness, harness, provider_model_id, litellm_model,
                    display_name, backend, context_window,
                    json.dumps({"resolved_identity": provider_model_id}),
                    now, now, now, now,
                )
                for (
                    model_id, harness, provider_model_id, litellm_model,
                    display_name, backend, context_window,
                ) in harness_rows
            ),
        )
    connect_fixture_harnesses(db, fixture_binaries, harness_rows, now)
    db.commit()
    db.close()

    logger.info("Mock catalog seeded: mock/test-model")


router = APIRouter(prefix="/api/test", tags=["test"])
router.include_router(pressure_fixture.router)


@router.post("/inject-message")
async def inject_message(token: str, body: dict) -> dict:
    """Inject a pre-built message row directly into a session for E2E testing."""
    import uuid
    from fastapi import HTTPException
    from infra.auth import authenticate
    from infra.database import get_db, write_transaction
    from infra.db_async import run_in_db
    from infra.queries import insert_message
    from infra.time import utc_now_iso
    from infra import state

    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    thread_id = body.get("thread_id") or ""
    msg = body.get("message") or {}
    if not thread_id or not msg:
        raise HTTPException(400, "thread_id and message are required")

    # Any valid token must not be able to inject into another user's session.
    def _check_ownership():
        db = get_db()
        try:
            return db.execute(
                "SELECT id FROM threads WHERE id = ? AND user_id = ?",
                (thread_id, user["id"]),
            ).fetchone() is not None
        finally:
            db.close()

    if not await run_in_db(_check_ownership):
        raise HTTPException(404, "Thread not found")

    mid = str(uuid.uuid4())
    now = utc_now_iso()
    role = msg.get("role", "system")
    content = msg.get("content", "")
    metadata = msg.get("metadata")
    if role in ("assistant", "tool") and not {"turn_id", "turn_unbound_reason"} & (metadata or {}).keys():
        metadata = {**(metadata or {}), "turn_unbound_reason": "legacy_import"}  # fixtures have no live Turn
    def _write():
        with write_transaction(None, scope="e2e_mock_message") as db:
            insert_message(thread_id, role, content,
                           metadata if metadata is not None else None,
                           msg_id=mid, db=db, created_at=now)

    await run_in_db(_write)
    state.broadcast_to_thread(thread_id, {
        "type": "message_added",
        "thread_id": thread_id,
        "message": {"id": mid, "role": role, "content": content,
                    "metadata": metadata, "created_at": now},
    })
    return {"ok": True, "message_id": mid}


@router.post("/mock-gates/{gate_name}/release")
async def release_mock_gate(gate_name: str) -> dict:
    """Release one response or completion gate seeded for the current E2E mock queue."""
    if not _release_mock_gate(gate_name):
        raise HTTPException(404, f"Unknown mock gate: {gate_name}")
    return {"ok": True, "gate": gate_name}


@router.get("/mock-gates/{gate_name}/wait")
async def wait_for_mock_gate(gate_name: str) -> dict:
    """Let an isolated fixture process await the same deterministic gate."""
    event = _mock_gates.get(gate_name)
    if event is None:
        raise HTTPException(404, f"Unknown mock gate: {gate_name}")
    await event.wait()
    return {"ok": True, "gate": gate_name}


@router.get("/harness-responses/{harness}/next")
async def next_harness_response(harness: str) -> dict:
    """Serve one seeded reply to a network-confined CLI fixture process."""
    if harness not in {"claude-code", "codex"}:
        raise HTTPException(404, f"Unknown fixture harness: {harness}")
    return {"reply": _next_reply(stream=True)}


@router.get("/mock-responses")
async def read_mock_response_state() -> dict:
    """Expose deterministic call count for no-retry-loop E2E assertions, and the calls no seeded reply served."""
    return {"call_count": _call_count, "unmatched_calls": mock_calls.unmatched}


@router.post("/mock-responses")
async def seed_mock_responses(body: dict) -> dict:
    """Seed mock LLM responses for E2E tests.

    Body: ``{"replies": [{"content": "...", "reasoning_content": "...", ...}]}``

    ``response_gate`` holds a call before it responds, ``completion_gate`` after
    its reasoning, until ``/mock-gates/{name}/release``. ``match_thread`` binds a
    reply to one native thread's turns. A seed replaces queue and gates.
    """
    replies = body.get("replies", [])
    _reset_mock_state(replies)
    return {"ok": True, "queued": len(replies)}
