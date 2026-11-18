"""End-to-end wiring proof: the attention notice reaches model text on both paths.

``test_attention_notice.py`` covers the module's own logic. This file proves
the two call sites deliver it: native ``core.turns_steps._append_tool_results``
and the harness bridge's real ``_with_notice_and_marker`` through
``subagents.harness_tool_publication._foreground_result``, including a call
that reaches its tool through the ``tool_call`` wrapper.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from types import SimpleNamespace

import pytest

import core.attention_notice as mod
from infra.schema import SCHEMA_SQL
from plans import service
from registry import models as registry_models
from tests.plan_close_next_helpers import owner_plan, plan_update, task
from tests.test_plan_reconcile_release import seed_threads


class _Open:
    """A shared connection, minus the close the notice does on its own handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self) -> None:
        return None


@pytest.fixture
def db(monkeypatch):
    """An in-memory plan database both projection paths can read from their worker thread."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    seed_threads(connection)
    monkeypatch.setattr(mod, "get_db", lambda: _Open(connection))
    monkeypatch.setattr(mod, "emit_product_event", lambda *_a, **_k: True)
    yield connection
    connection.close()


def _raised_ask(db, tmp_path) -> dict:
    """Raise a real document ask as ``owner``; return the ``plan_update`` arguments that did it."""
    path = tmp_path / "proposal.md"
    path.write_text(" ".join(["word"] * 500), encoding="utf-8")
    artifact = registry_models.register_artifact(
        integration="local", type="document", external_id=str(path),
        parent_thread_id="owner", title=path.name, path=str(path),
        metadata={"zone": "workspace"}, db=db,
    )
    plan = owner_plan(db)
    node = task(db, plan.id, "Proposal")
    params = {
        "node_id": node.id, "expected_revision": service._node_row(db, node.id)["revision"],
        "reason_key": "review", "reason": "Review it", "owner_kind": "user",
        "ask_artifact_id": str(artifact["id"]),
    }
    plan_update(db, plan.id, "block_node", params)
    return {"plan_id": plan.id, "operation": "block_node", "params": params}


_ASK_NOTICE = (
    "[CORSIA attention notice] The person now has 'proposal.md' to read before they can "
    "answer: 500 words, at least 3 minutes to read."
)


@pytest.mark.asyncio
async def test_native_path_attaches_the_notice_to_the_ask_result(db, tmp_path, monkeypatch):
    from core import turns_steps
    from core.context_builder import ContextBuilder

    arguments = _raised_ask(db, tmp_path)
    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(turns_steps, "_native_tool_media", lambda *_a, **_k: None)

    canonical = SimpleNamespace(role="tool", content='{"ok": true}', turn_id="turn-1")
    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [],
        [(
            SimpleNamespace(id="call-1", name="plan_update", arguments=arguments),
            '{"ok": true}',
            canonical,
        )],
        "owner",
        512,
        "anthropic/selected",
    )

    content = messages[0]["content"]
    assert content.startswith('{"ok": true}')
    assert _ASK_NOTICE in content


@pytest.mark.asyncio
async def test_native_path_reads_document_opens_outcome_from_the_canonical_result(
    db, tmp_path, monkeypatch,
):
    """A relative path resolves against the agent's workspace; a refused open says nothing."""
    from core import turns_steps
    from core.context_builder import ContextBuilder

    (tmp_path / "report.md").write_text(" ".join(["word"] * 100), encoding="utf-8")
    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(turns_steps, "_native_tool_media", lambda *_a, **_k: None)

    def _result(call_id: str, outcome: str):
        content = json.dumps({"outcome": outcome, "reason": None})
        return (
            SimpleNamespace(id=call_id, name="document_open", arguments={"path": "report.md"}),
            content,
            SimpleNamespace(role="tool", content=content, turn_id="turn-1"),
        )

    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [],
        [_result("call-1", "requested"), _result("call-2", "refused")],
        "owner",
        512,
        "anthropic/selected",
    )

    opened, refused = (message["content"] for message in messages)
    assert "You put 'report.md' in front of the person: 100 words" in opened
    assert mod.LABEL not in refused


async def _bridge_result(name: str, arguments: dict, text: str, *, workspace=None) -> str:
    from subagents.harness_tool_bridge import _with_notice_and_marker
    from subagents.harness_tool_publication import _foreground_result
    from tools.recorded_call import CompletedToolCall

    async def persist_delivery(**_kwargs):
        return SimpleNamespace(
            invocation_id="inv", assistant_step_id="step",
            render_for_model=lambda **_kw: text,
        )

    async def media_delivery(_delivery, _model):
        return None

    return await _foreground_result(
        CompletedToolCall(name=name, result=text, invocation_id="inv", arguments=arguments),
        thread_id="owner", turn_identity=SimpleNamespace(turn_id="turn-1"),
        model=None, persist_delivery=persist_delivery, media_delivery=media_delivery,
        decorate_result=functools.partial(_with_notice_and_marker, workspace=workspace),
    )


@pytest.mark.asyncio
async def test_harness_bridge_attaches_the_notice_to_the_ask_result(db, tmp_path, monkeypatch):
    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)
    arguments = _raised_ask(db, tmp_path)

    result = await _bridge_result("plan_update", arguments, '{"ok": true}')

    assert result.startswith('{"ok": true}')
    assert _ASK_NOTICE in result


@pytest.mark.asyncio
async def test_harness_bridge_sees_through_the_tool_call_wrapper(db, tmp_path, monkeypatch):
    """``document_open`` is reached through ``tool_call``: the notice reads the tool it dispatches to."""
    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)
    (tmp_path / "report.md").write_text(" ".join(["word"] * 100), encoding="utf-8")

    result = await _bridge_result(
        "tool_call",
        {"name": "document_open", "arguments": {"path": "report.md"}},
        json.dumps({"outcome": "requested", "reason": None}),
        workspace=tmp_path,
    )

    assert "You put 'report.md' in front of the person: 100 words" in result


@pytest.mark.asyncio
async def test_harness_bridge_attaches_the_notice_to_an_update_that_links_a_document(
    db, tmp_path, monkeypatch,
):
    """The bridge hands the notice ``post_update``'s own receipt, which names the saved update."""
    from infra.queries import insert_message
    from tests.project_ref_helpers import select_workspace
    from tools.post_update_text import result_text

    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)
    select_workspace(monkeypatch, tmp_path)
    (tmp_path.resolve() / "report.md").write_text(" ".join(["word"] * 100), encoding="utf-8")
    text = "The report is ready: report.md."
    message_id = insert_message("owner", "assistant", text, {"for_user": True, "turn_id": "turn-1"}, db=db)
    db.commit()
    receipt = result_text(message_id, [], [], "owner")

    result = await _bridge_result("post_update", {"text": text}, receipt)

    assert result.startswith(receipt)
    assert "Your update links 'report.md' for the person to read: 100 words" in result


@pytest.mark.asyncio
async def test_harness_bridge_adds_nothing_to_other_results(db, tmp_path, monkeypatch):
    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)

    result = await _bridge_result("read_file", {"path": str(tmp_path / "absent.md")}, "one\n")

    assert mod.LABEL not in result
