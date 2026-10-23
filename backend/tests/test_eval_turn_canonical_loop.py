"""POST /api/eval/turn runs its tool loop through the canonical Turn/step path.

The eval route used to drive the Native loop itself: ``run_turn_with_steps``
with a private ``_EvalStepCollector`` as ``on_step`` and no admitted Turn. A
Native tool loop always passes ``HARNESS_TOOL_RESULT_BUDGET_TOKENS`` into
``core.turns_steps.execute_tool_calls``, and that projection step requires the
canonical persisted tool row its ``tool_complete`` step returns
(``core.native_tool_result.handle_native_tool_complete``). The collector
returned ``None``, so the first tool-calling response failed the turn with
"Native tool projection requires its canonical persisted row"; and because no
Turn was admitted, the run left ``llm_usage.turn_id`` and
``context_manifest_id`` null.

This test runs the real route on a fresh database and fakes only the transport
under the provider barrier. It asserts the loop's durable effects, not the
route's frame bookkeeping: the tool row exists and comes back projected into
the second provider request, and the Turn's usage rows carry their durable
identity.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eval.null_tool import NULL_TOOL_NAME

MODEL = "openai/test-checkpoint"
PROMPT = "run the probe"
EVAL_TOKEN = "eval-test-token"
PROBE_ARGUMENTS = {"probe_id": "probe-1"}


class _RegisteredTools:
    """The slice of the tool registry the eval route and the loop actually use."""

    def __init__(self):
        self._registered: dict = {}

    def has(self, name: str) -> bool:
        return name in self._registered

    def register(self, tool) -> None:
        self._registered[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._registered.pop(name, None)

    def get_definitions(self) -> list:
        return []

    async def execute(self, name: str, arguments: dict) -> str:
        return await self._registered[name].execute(**arguments)


class _Agent:
    model = MODEL
    max_iterations = 2
    max_tokens = 100

    def __init__(self, workspace):
        from core.context_builder import ContextBuilder

        self.context = ContextBuilder(workspace)
        self.workspace = workspace
        self.tools = _RegisteredTools()

    async def _connect_mcp(self):
        return None

    def _strip_think(self, content):
        return content

    def _tool_hint(self, tool_calls) -> str:
        return ", ".join(tc.name for tc in tool_calls)


@pytest.fixture
def eval_client(isolated_db, monkeypatch, tmp_path):
    """The real eval route, a real database, and a recording transport."""
    from core import turns_steps
    from eval import endpoint
    from llm import router

    dispatched: list[dict] = []
    responses: list = []

    async def dispatch_openai(messages, tools, **kwargs):
        dispatched.append({"messages": messages, "tools": tools, **kwargs})
        return responses.pop(0)

    agent = _Agent(tmp_path)
    monkeypatch.setattr(endpoint, "authenticate", lambda _token: {"id": "steve"})
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(router, "_dispatch_openai", dispatch_openai)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    app = FastAPI()
    app.include_router(endpoint.router)
    with TestClient(app) as client:
        yield client, dispatched, responses


def _post(client):
    return client.post(
        "/api/eval/turn",
        params={"token": EVAL_TOKEN},
        json={"input": PROMPT, "metadata": {
            "model": MODEL, "timeout_seconds": 30,
            "tools_in_scope": [NULL_TOOL_NAME],
        }},
    )


def _tool_call_response():
    from llm import router

    return router.LLMResponse(
        content="",
        tool_calls=[router.ToolCallRequest(
            id="call-1", name=NULL_TOOL_NAME, arguments=dict(PROBE_ARGUMENTS),
        )],
    )


def _answer_response():
    from llm import router

    return router.LLMResponse(
        content="probe answered",
        usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    )


def _rows(query: str):
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(query).fetchall()]
    finally:
        db.close()


def test_eval_tool_loop_persists_its_canonical_row_and_projects_it(eval_client):
    client, dispatched, responses = eval_client
    responses.extend([_tool_call_response(), _answer_response()])

    body = _post(client).json()

    assert body["error"] is None, body["error"]
    assert body["content"] == "probe answered"
    assert [call["name"] for call in body["tool_calls"]] == [NULL_TOOL_NAME]

    # The second provider request is built from the projected canonical row.
    assert len(dispatched) == 2
    second_messages = dispatched[1]["messages"]
    tool_messages = [m for m in second_messages if m.get("role") == "tool"]
    assert tool_messages, [m.get("role") for m in second_messages]
    assert PROBE_ARGUMENTS["probe_id"] in tool_messages[-1]["content"]


def test_eval_turn_does_not_claim_during_restart_hold(eval_client, monkeypatch):
    client, _dispatched, _responses = eval_client
    monkeypatch.setattr(
        "automation.self_restart.restart_holds_new_work",
        lambda *, db=None: True,
    )

    body = _post(client).json()

    assert body["error"] == "Eval turn is held during restart"
    assert _rows("SELECT id FROM active_turns") == []


def test_eval_tool_loop_leaves_durable_turn_attribution(eval_client):
    client, _dispatched, responses = eval_client
    responses.extend([_tool_call_response(), _answer_response()])

    assert _post(client).json()["error"] is None

    tool_rows = _rows(
        "SELECT id, json_extract(metadata,'$.turn_id') AS turn_id "
        "FROM messages WHERE role='tool'"
    )
    assert len(tool_rows) == 1
    assert tool_rows[0]["turn_id"]

    usage_rows = _rows("SELECT turn_id,context_manifest_id FROM llm_usage")
    assert len(usage_rows) == 2
    assert all(row["turn_id"] for row in usage_rows), usage_rows
    assert all(row["context_manifest_id"] for row in usage_rows), usage_rows

    manifest_ids = {row["id"] for row in _rows("SELECT id FROM context_manifests")}
    assert manifest_ids, "the Turn's context manifest row was never written"
    assert {row["context_manifest_id"] for row in usage_rows} <= manifest_ids


def test_eval_response_reports_the_canonical_usage(eval_client):
    client, _dispatched, responses = eval_client
    responses.extend([_tool_call_response(), _answer_response()])

    body = _post(client).json()

    assert body["error"] is None, body["error"]
    assert body["usage"]["prompt_tokens"] == 11
    assert body["usage"]["completion_tokens"] == 3