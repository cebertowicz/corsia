"""Artifact handoff authority through each harness's real tool entry point (#2691, #2643).

A Codex worker that owned its PR passed a free-text justification as
``authority``; the backend treated the unknown value as a permission denial,
which read as a thread-identity fault. Identity binding is correct on both
bridges; an unknown authority is a parameter error naming the accepted values.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.registry_helpers import seed_artifact
from tests.test_cc_tool_result_echo_binding import _seed_thread

PR = {
    "integration": "github",
    "type": "pull-request",
    "external_id": "cebertowicz/corsia#2659",
}
FREE_TEXT = "Agent Substrate explicitly accepted review ownership and requested transfer."


def _seed_owned_pr(db) -> None:
    """thread-1 (the worker) owns the PR; parent-1 is its parent."""
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('parent-1','user-1','claude-code/sonnet','t','t')"
    )
    seed_artifact(
        db, artifact_id="pr-2659", parent_thread_id="thread-1",
        title="PR #2659", **PR,
    )
    db.commit()


def _owner(db) -> str:
    from registry import models

    routing = models.routing_registrations(
        PR["integration"], PR["type"], PR["external_id"], db=db,
    )
    return routing["owner"]["parent_thread_id"]


def _rehome_args(authority: str | None) -> dict:
    args = {**PR, "new_owner_thread_id": "parent-1", "reason": "hand PR events to parent"}
    if authority is not None:
        args["authority"] = authority
    return args


async def _codex_call(db, tmp_path, arguments: dict) -> str:
    from subagents import codex_dynamic_tools
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import build_tool_registry

    identity = _seed_thread(db)
    _seed_owned_pr(db)
    step = persist_assistant_step("thread-1", content="", tool_uses=[{
        "id": "call", "name": "artifact_rehome", "arguments": arguments,
    }], turn_id=identity.turn_id, db=db)
    db.commit()
    session = SimpleNamespace(
        checkpoint_id="checkpoint", checkpoint_tool_result_fits=None,
        tool_registry=build_tool_registry(str(tmp_path)),
        corsia_thread_id="thread-1", corsia_turn_identity=identity,
        declared_tool_step_ids={"call": step}, catalog_model_id=None, model=None,
    )
    reply, _ = await codex_dynamic_tools.execute_dynamic_tool(
        session, "artifact_rehome", arguments, {"callId": "call"},
    )
    return reply["contentItems"][0]["text"]


async def _cc_call(db, tmp_path, arguments: dict) -> str:
    from api import cc_attach
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.harness_tool_bridge import build_tool_registry

    identity = _seed_thread(db)
    _seed_owned_pr(db)
    live = _Live(
        session=SimpleNamespace(
            record_tool_reply=AsyncMock(), tool_context=SimpleNamespace(tokens=100),
        ),
        parent_thread_id="parent-1",
        name="cc",
        corsia_turn=identity,
        tool_registry=build_tool_registry(str(tmp_path)),
        turn_active=True,
        last_call_usage={"input_tokens": 100},
        context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put("thread-1", live)
    try:
        on_event = _make_on_event(live, "thread-1", lambda *_args: None, db)
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "provider-call", "name": "mcp__corsia__artifact_rehome",
                "input": arguments,
            }],
        })
        return await cc_attach._execute_admitted_tool(live, "thread-1", {
            "invocation_id": "invocation-call",
            "provider_tool_call_id": "provider-call",
            "name": "artifact_rehome",
            "arguments": arguments,
        })
    finally:
        _LIVE.pop("thread-1", None)


_HARNESSES = {"codex": _codex_call, "claude-code": _cc_call}


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", sorted(_HARNESSES))
@pytest.mark.parametrize("authority", [None, "current-owner-handoff"])
async def test_owning_worker_hands_pr_to_parent(isolated_db, tmp_path, harness, authority):
    from infra.database import get_db

    db = get_db()
    text = await _HARNESSES[harness](db, tmp_path, _rehome_args(authority))

    assert not text.startswith("Error"), text
    assert _owner(db) == "parent-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", sorted(_HARNESSES))
async def test_unknown_authority_is_a_parameter_error_not_a_denial(isolated_db, tmp_path, harness):
    from infra.database import get_db

    db = get_db()
    text = await _HARNESSES[harness](db, tmp_path, _rehome_args(FREE_TEXT))

    assert "authority denied" not in text, text
    assert "Invalid parameters for tool 'artifact_rehome'" in text
    assert _owner(db) == "thread-1"
    from registry.ownership_authority import OWNERSHIP_AUTHORITIES

    assert all(value in text for value in OWNERSHIP_AUTHORITIES)


@pytest.mark.parametrize("tool_name", ["artifact_rehome", "artifact_release"])
def test_advertised_schema_enumerates_authority(tmp_path, tool_name):
    from subagents.harness_tool_bridge import build_tool_registry

    authority = build_tool_registry(str(tmp_path)).get(tool_name).parameters[
        "properties"
    ]["authority"]

    assert "enum" in authority, json.dumps(authority)
    assert "current-owner-handoff" in authority["description"]
    from registry.ownership_authority import OWNERSHIP_AUTHORITIES

    assert authority["enum"] == [*OWNERSHIP_AUTHORITIES, None]
