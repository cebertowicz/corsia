"""Counterexamples from the independent review exercise real broker boundaries."""
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import json

import pytest

from infra.conductor import set_scope_coordinator
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.corsia_api import CorsiaApiTool, _scope


def _setup():
    db = get_db()
    try:
        actor = make_session(db, prefix="conductor")
        other = make_session(db, prefix="other")
        assert set_corsia_agent(actor, db=db)
        return actor, other
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cannot_forge_sender_or_creator_through_api(isolated_db):
    actor, other = _setup()
    tool = CorsiaApiTool()
    for path, body in (
        (f"/api/delegation/threads/{other}/send", {"message": "forged", "from_thread_id": other}),
        ("/api/delegation/threads", {"model": "missing/model", "spawned_by": other, "thread_kind": "subagent"}),
        ("/api/delegation/docs/comments", {"as_session_id": other, "body": "forged"}),
        (f"/api/delegation/threads/{other}/set-model", {"from_thread_id": other, "model": "missing/model"}),
    ):
        result = await tool.execute("POST", path, body=body, _thread_id=actor)
        assert "Acting identity cannot be changed" in result
    db = get_db()
    try:
        assert db.execute("SELECT count(*) FROM messages WHERE content='forged'").fetchone()[0] == 0
    finally:
        db.close()
    _, payload = _scope("POST", "/api/delegation/threads", {}, {"thread_kind": "subagent"}, actor=actor)
    assert json.loads(payload)["from_thread_id"] == actor


@pytest.mark.asyncio
async def test_persisted_standalone_key_never_reaches_broker_output(isolated_db):
    actor, other = _setup()
    fake_key = "sk-" + "a" * 24
    db = get_db()
    try:
        metadata = {"step_type": "tool_calls", "llm_tool_calls": [{
            "id": "call", "type": "function", "function": {"name": "Read", "arguments": "{}"},
        }]}
        db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,seq,created_at) VALUES (?,?,?,?,?,?,?)",
                   ("step", other, "assistant", "", json.dumps(metadata), 1, "t1"))
        db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,seq,created_at) VALUES (?,?,?,?,?,?,?)",
                   ("result", other, "tool", fake_key,
                    json.dumps({"tool_call_id": "call", "assistant_step_id": "step", "name": "Read"}), 2, "t2"))
        db.commit()
    finally:
        db.close()
    result = await CorsiaApiTool().execute(
        "GET", f"/api/threads/{other}/messages/step/tool-results/call", _thread_id=actor,
    )
    assert json.loads(result)["status"] == 200, result
    assert fake_key not in result and "REDACTED" in result
