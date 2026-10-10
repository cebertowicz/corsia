"""The eval route opens its thread, reads its replay, and freezes a fit ledger.

An eval turn once failed before provider contact twice over: the route passed
no ``prepared_context`` (the provider-call barrier rejected the missing fit
ledger), and once it did, it passed ``replay_selection=None`` (the Native
prefix build rejects that) for a thread that had no row to read a replay from.
The route runs here for real against a fresh database: thread creation, the
canonical replay read, the real prefix build, the fit ledger and the barrier.
Only the transport under the barrier is faked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODEL = "openai/test-checkpoint"
PROMPT = "say hello"
TINY_WINDOW = 20_000


class _Tools:
    def get_definitions(self):
        return []


class _Agent:
    model = MODEL
    max_iterations = 2
    max_tokens = 100
    tools = _Tools()

    def __init__(self, workspace):
        from core.context_builder import ContextBuilder

        self.context = ContextBuilder(workspace)

    async def _connect_mcp(self):
        return None

    def _strip_think(self, content):
        return content


@pytest.fixture
def eval_client(isolated_db, monkeypatch, tmp_path):
    """The real eval route on a fresh database, with a recording transport."""
    from core import turns_steps
    from core.agent import get_agent  # noqa: F401  (patched below by name)
    from eval import endpoint
    from llm import router

    dispatched: list[dict] = []

    async def dispatch_openai(messages, tools, **kwargs):
        dispatched.append({"messages": messages, "tools": tools, **kwargs})
        return router.LLMResponse(content="hello", usage={})

    agent = _Agent(tmp_path)
    monkeypatch.setattr(endpoint, "authenticate", lambda _token: {"id": "steve"})
    monkeypatch.setattr(endpoint, "_build_eval_agent", AsyncMock(return_value=(agent, [])))
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(router, "_dispatch_openai", dispatch_openai)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    app = FastAPI()
    app.include_router(endpoint.router)
    with TestClient(app) as client:
        yield client, dispatched


def _post(client, model=MODEL, prompt=PROMPT):
    return client.post(
        "/api/eval/turn?token=t",
        json={"input": prompt, "metadata": {"model": model, "timeout_seconds": 30}},
    )


def test_eval_turn_reaches_the_provider_with_a_fit_ledger(eval_client):
    client, dispatched = eval_client

    body = _post(client).json()

    assert body["error"] is None, body["error"]
    assert body["content"] == "hello"
    assert len(dispatched) == 1
    assert dispatched[0]["messages"][-1]["content"] == PROMPT


def test_eval_turn_thread_exists_for_the_replay_read(eval_client):
    from infra.database import get_db

    client, _dispatched = eval_client

    assert _post(client).json()["error"] is None

    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, user_id, model FROM threads WHERE id LIKE 'eval-%'"
        ).fetchall()
    finally:
        db.close()
    assert [(row["user_id"], row["model"]) for row in rows] == [("steve", MODEL)]


def test_eval_turn_refuses_a_payload_that_does_not_fit(eval_client, monkeypatch):
    from dataclasses import replace

    from infra import state
    from infra.database import get_db
    from native_checkpoint_fixtures import native_target

    client, dispatched = eval_client
    # The canonical turn engine rechecks its resolved Native target before
    # adapter use (``ws.turn._setup_turn`` -> ``llm.resolution.validate_native_target``),
    # so the synthetic target has to declare the cloud backend it stands for.
    # What must not fit is the payload, not the model: a 20k window still holds
    # a checkpoint prompt, so admission accepts the input, but the input alone
    # is larger than the window, so no continuation can carry it.
    tiny = replace(native_target(context_window=TINY_WINDOW), backend="api")
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model, **_kw: tiny,
    )
    # The live window must agree with the target. Refusal settlement rereads
    # it (catalog row, then the seeded profile override), and a mismatch reads
    # as a cap change that outran the refusal: the input is released and
    # retried forever, and the retrying turn outlives this test's loop (#3147).
    db = get_db()
    try:
        db.execute(
            "UPDATE models SET context_window=? WHERE id=?", (TINY_WINDOW, MODEL),
        )
        db.execute("DELETE FROM model_profiles WHERE model_id=?", (MODEL,))
        db.commit()
    finally:
        db.close()

    body = _post(client, prompt="word " * (2 * TINY_WINDOW)).json()

    # The refusal now comes from the canonical checkpoint/continuation guard the
    # turn runs before its provider call (``core.checkpoint_resume``), not from
    # the route's own pre-canonical payload preflight, so the vocabulary is the
    # platform's: the window is named, and nothing was dispatched.
    assert "exceeds the context window" in (body["error"] or "")
    assert dispatched == []
    # A deterministic refusal fails its input visibly and leaves no Native owner.
    db = get_db()
    try:
        states = db.execute(
            "SELECT delivery_state FROM messages "
            "WHERE thread_id LIKE 'eval-%' AND role='user'"
        ).fetchall()
    finally:
        db.close()
    assert [row["delivery_state"] for row in states] == ["failed"]
    residue = [
        thread_id
        for registry in (state._active_turns, state._active_turn_tasks, state._active_llm_tasks)
        for thread_id in registry
        if thread_id.startswith("eval-")
    ]
    assert residue == []
