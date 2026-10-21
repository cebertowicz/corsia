"""Disposable fork policy at real Native, CC bridge and Codex control seams."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from core import fork_runtime
from starlette.requests import Request

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from infra.database import write_transaction
from core.fork_runtime import ForkKindDefinitionError, handles
from core.fork_tools import execute_fork_tool
from test_doc_review_requests import request, review_env, fork_comment, stored


def test_every_harness_requires_an_explicit_limit():
    with pytest.raises(ForkKindDefinitionError, match="missing step limits for: codex"):
        replace(ARTIFACT_REVIEW_KIND, step_limits={"native": 3, "claude-code": 3})


@pytest.mark.asyncio
async def test_native_refuses_unenforceable_limit_before_provider_call(monkeypatch):
    from core import fork_native as native
    from core.fork_runtime import ForkStepLimitError
    from test_checkpoint_provider_adapters import _request, _source

    async def agent():
        return SimpleNamespace()

    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_provider_params", lambda *_args: {})
    kind = replace(ARTIFACT_REVIEW_KIND, step_limits={"native": None, "claude-code": 3, "codex": 3})
    with pytest.raises(ForkStepLimitError, match="has no Native step limit"):
        await native.invoke_fork(_request(_source(native)), asyncio.Event(), kind=kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["delegate_send", "delegate_task", "write_file", "tool_call"])
async def test_review_refuses_send_spawn_and_file_write_before_dispatch(name):
    calls = []

    async def execute(*args):
        calls.append(args)
        return "unexpected"

    result = await execute_fork_tool(
        ARTIFACT_REVIEW_KIND, SimpleNamespace(thread_id="reviewer"),
        SimpleNamespace(execute=execute), asyncio.Event(), name, {},
    )
    assert result == ARTIFACT_REVIEW_KIND.tool_refusal
    assert calls == []


def test_read_cannot_bypass_document_scope_with_an_owned_parent(review_env):
    from doceditor.review_fork_scope import authorize_tool

    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    comment = db.execute("SELECT id FROM doc_comments WHERE artifact_id=?", (aid,)).fetchone()
    with pytest.raises(PermissionError, match="only read its requested document"):
        authorize_tool(SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir="/workspace"),
                       "read_file", {"path": "/workspace/shared/unrelated.md", "parent_id": comment["id"]})


@pytest.mark.asyncio
async def test_cc_bridge_owns_token_allowlist_exact_call_and_cancel(review_env):
    from core.fork_cc_bridge import authenticate, bind_bridge, definitions, execute
    from test_checkpoint_provider_adapters import _request
    from fastapi import HTTPException

    db, aid = review_env
    row = request(db, aid)
    provider = replace(
        _request(None), fork_id=row["fork_id"], thread_id="reviewer", mcp_config="fixture",
    )
    with bind_bridge(provider, ARTIFACT_REVIEW_KIND, asyncio.Event()) as (_request_, handle):
        http = Request({"type": "http", "client": ("127.0.0.1", 42),
                        "headers": [(b"x-corsia-fork-id", row["fork_id"].encode())]})
        assert authenticate(http, "reviewer", handle.tool_token) is handle
        with open(_request_.mcp_config) as stream:
            env = json.load(stream)["mcpServers"]["corsia"]["env"]
        assert env["CORSIA_FORK_ID"] == row["fork_id"]
        assert env["CORSIA_FORK_TOOL_TOKEN"] == handle.tool_token
        with pytest.raises(HTTPException):
            authenticate(http, "reviewer", "forged")
        advertised = {item["function"]["name"] for item in definitions(handle)}
        assert advertised <= ARTIFACT_REVIEW_KIND.tool_allowlist
        assert "doc_feedback_send" in advertised

        async def bound(operation, call_id, name, arguments):
            assert call_id == "declared"
            return await operation()

        handle.tool_executor = bound
        result = await execute(handle, {"provider_tool_call_id": "declared", "name": "write_file"})
        assert result == ARTIFACT_REVIEW_KIND.tool_refusal
        handle.cancel_event.set()
        with pytest.raises(HTTPException):
            authenticate(http, "reviewer", handle.tool_token)
    assert row["fork_id"] not in handles


@pytest.mark.asyncio
async def test_codex_counts_only_distinct_owned_completed_items_and_terminates():
    from core.fork_codex_control import CompletedItemLimit

    killed = []
    session = SimpleNamespace(
        _is_active_provider_boundary=lambda thread, turn: (thread, turn) == ("owned", "turn"),
        revoke_dynamic_tool_admission=lambda: killed.append("revoke"),
        _request_terminate=lambda: killed.append("terminate"),
    )
    limit = CompletedItemLimit(replace(ARTIFACT_REVIEW_KIND, step_limits={
        "native": 2, "claude-code": 2, "codex": 2,
    }), None)

    def frame(item, thread="owned", turn="turn", method="item/completed"):
        return {"method": method, "params": {"threadId": thread, "turnId": turn,
                "item": {"id": item, "type": "dynamicToolCall"}}}

    await limit.observe(session, frame("old", turn="previous"))
    await limit.observe(session, frame("foreign", thread="other"))
    await limit.observe(session, frame("a", method="item/started"))
    await limit.observe(session, frame("a"))
    await limit.observe(session, frame("a"))
    assert not limit.reached.is_set() and killed == []
    await limit.observe(session, frame("b"))
    assert limit.reached.is_set() and killed == ["revoke", "terminate"]
    assert "fork_step_limit_reached" in str(limit.failure())


@pytest.mark.asyncio
async def test_codex_cancel_closes_exact_session_while_start_is_waiting():
    from core.fork_codex_control import CompletedItemLimit, run_session
    from core.fork_codex_result import CodexForkCapture

    started, cancel = asyncio.Event(), asyncio.Event()
    closed = []

    async def start():
        started.set()
        await asyncio.Event().wait()

    async def terminate():
        closed.append(True)

    session = SimpleNamespace(start=start, terminate=terminate)
    task = asyncio.create_task(run_session(
        session, CodexForkCapture(kind=ARTIFACT_REVIEW_KIND), CompletedItemLimit(ARTIFACT_REVIEW_KIND, None), cancel,
    ))
    await started.wait()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


@pytest.mark.asyncio
async def test_codex_missing_completed_identity_wakes_and_closes_waiting_session():
    from core.fork_codex_control import CompletedItemLimit, ForkItemLimitReached, run_session
    from core.fork_codex_result import CodexForkCapture

    started = asyncio.Event()
    closed = []

    async def start():
        started.set()
        return True

    async def terminate():
        closed.append(True)

    session = SimpleNamespace(
        start=start, terminate=terminate,
        _is_active_provider_boundary=lambda *_args: True,
        revoke_dynamic_tool_admission=lambda: None,
        _request_terminate=lambda: None,
    )
    limit = CompletedItemLimit(ARTIFACT_REVIEW_KIND, None)
    task = asyncio.create_task(run_session(
        session, CodexForkCapture(kind=ARTIFACT_REVIEW_KIND), limit, asyncio.Event(),
    ))
    await started.wait()
    await limit.observe(session, {"method": "item/completed", "params": {"item": {"type": "dynamicToolCall"}}})
    with pytest.raises(ForkItemLimitReached, match="fork_completed_item_missing_identity"):
        await asyncio.wait_for(task, 1)
    assert closed == [True]


@pytest.mark.asyncio
async def test_busy_reviewer_pass_writes_as_reviewer_without_turn_or_node(review_env, monkeypatch):
    from core import fork_native as native
    from core.artifact_review_runtime import _run
    from llm.router import LLMResponse, ToolCallRequest
    from tools.documents import DocCommentCreateTool
    from tools.document_feedback import DocFeedbackSendTool
    from tools.registry import ToolRegistry
    from test_checkpoint_provider_adapters import _request
    from infra import state

    db, aid = review_env
    row = request(db, aid)
    registry = ToolRegistry()
    registry.register(DocCommentCreateTool())
    registry.register(DocFeedbackSendTool())
    provider = replace(_request(None), fork_id=row["fork_id"], thread_id="reviewer")
    replies = iter([
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="comment", name="doc_comment_create", arguments={
            "artifact_id": aid, "body": "The claim needs evidence.", "review": True, "client_id": "fork-proof",
        })]),
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="submit", name="doc_feedback_send", arguments={
            "artifact_id": aid,
        })]),
        LLMResponse(content="Review submitted."),
    ])

    async def agent():
        return SimpleNamespace(max_iterations=3, tools=registry)

    async def complete(**kwargs):
        assert state._current_thread_id.get() == "reviewer"
        return next(replies)

    async def busy_source(request_):
        return request_

    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_provider_params", lambda *_args: {})
    monkeypatch.setattr(native, "_chat_completion", complete)
    monkeypatch.setattr("core.artifact_review_runtime._source", busy_source)
    before_turns = db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='reviewer'").fetchone()[0]
    await _run(row["id"], provider, asyncio.Event())
    comment = db.execute("SELECT * FROM doc_comments WHERE body='The claim needs evidence.'").fetchone()
    assert comment is not None and comment["author_thread_id"] == "reviewer"
    assert stored(db, row["id"])["answered_batch_id"]
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE owner_thread_id='reviewer'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='reviewer'").fetchone()[0] == before_turns



def test_submitted_receipt_and_owner_reply_reach_next_reviewer_context(review_env):
    from infra.passive_changes import materialize_pending_digest
    import json
    from doc_feedback_helpers import add_comment
    from doceditor import review_feedback, review_requests

    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    with write_transaction(db, scope="test_receipt_submit"):
        review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE owner_thread_id='reviewer'").fetchone()[0] == 0
    comment = db.execute("SELECT id FROM doc_comments WHERE author_thread_id='reviewer'").fetchone()
    digest = materialize_pending_digest(db, "reviewer")
    db.commit()
    assert digest is not None
    metadata = json.loads(db.execute("SELECT metadata FROM messages WHERE id=?", (digest.message_id,)).fetchone()[0])
    assert any("Document review submitted" in event["summary"] for event in metadata["context_events"])
    # The ordinary owner reply still routes to the fork comment's root author.
    reply, _ = add_comment(db, aid, author_id="owner", parent_id=comment["id"], submit=True)
    delivered = db.execute(
        "SELECT b.delivered_node_id FROM doc_feedback_batches b "
        "JOIN doc_feedback_batch_items i ON i.batch_id=b.id WHERE i.comment_id=? AND b.recipient_id='reviewer'",
        (reply["id"],),
    ).fetchone()
    assert delivered and delivered["delivered_node_id"]


def test_foreground_completion_delivers_fork_feedback_through_ordinary_batch(review_env):
    from doceditor import feedback_batches

    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    with write_transaction(db, scope="test_foreground_end_during_fork"):
        feedback_batches.close_for_turn_end(db, "reviewer")
    assert feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db) == []
    assert db.execute("SELECT state,item_count FROM doc_feedback_batches").fetchone()[:] == ("delivered", 1)
    assert stored(db, row["id"])["answered_batch_id"]
    assert stored(db, row["id"])["state"] == "submitted"


def test_startup_escalates_each_orphaned_review_once(review_env):
    """R: a fork that died with the process escalates once; settled requests stay."""
    from core.artifact_review_capture import reconcile_startup_reviews
    from doc_feedback_helpers import seed_thread
    from doceditor import review_feedback, review_requests

    db, aid = review_env
    seed_thread(db, "third", title="Third")
    db.execute("UPDATE threads SET agent_enabled=1,agent_responsibility='Review documents' WHERE id='third'")
    db.commit()
    orphan = request(db, aid)
    fork_comment(db, aid, orphan["id"])
    cancelled = request(db, aid, reviewer="second", key="second-key")
    actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    review_requests.cancel(db, cancelled["id"], actor)
    answered = request(db, aid, reviewer="third", key="third-key")
    with write_transaction(db, scope="test_answered_review"):
        review_feedback.submit_fork_feedback(db, answered["id"], "third", aid)
    before = {rid: stored(db, rid) for rid in (cancelled["id"], answered["id"])}

    assert asyncio.run(reconcile_startup_reviews()) == [orphan["id"]]
    assert asyncio.run(reconcile_startup_reviews()) == []

    escalated = stored(db, orphan["id"])
    assert escalated["node_id"] and escalated["fork_id"] is None
    assert escalated["state"] == "handed_back" and escalated["reason"] == "interrupted"
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 1
    assert {rid: stored(db, rid) for rid in before} == before


@pytest.mark.asyncio
async def test_startup_hands_back_forked_rows_without_a_live_handle_filter(review_env):
    from core.artifact_review_capture import reconcile_startup_reviews

    db, aid = review_env
    live = request(db, aid)

    assert await reconcile_startup_reviews() == [live["id"]]
    recovered = stored(db, live["id"])
    assert recovered["node_id"]
    assert (recovered["state"], recovered["reason"]) == ("handed_back", "interrupted")


@pytest.mark.asyncio
async def test_shutdown_cancels_only_checkpoint_rows_and_counts_all_forks(monkeypatch):
    """(a): every fork stops, but a review fork has no checkpoint row to cancel."""
    from core import checkpoint_runtime
    from core.checkpoint_fork_kind import CHECKPOINT_KIND
    from core.fork_runtime import ForkHandle

    cancelled_rows = []
    monkeypatch.setattr(
        checkpoint_runtime.checkpoint_store, "cancel",
        lambda checkpoint_id, **_kw: cancelled_rows.append(checkpoint_id),
    )
    monkeypatch.setattr(fork_runtime, "handles", {})

    def fork(fork_id, kind):
        event = asyncio.Event()
        fork_runtime.handles[fork_id] = ForkHandle(
            asyncio.create_task(event.wait()), event, "thread", kind=kind,
        )
        return event

    events = [fork("cp-1", CHECKPOINT_KIND), fork("review-1", ARTIFACT_REVIEW_KIND)]
    assert fork_runtime.fork_task_count() == 2

    await fork_runtime.shutdown_forks()
    await checkpoint_runtime.shutdown_checkpoint_wakes()

    assert cancelled_rows == ["cp-1"]
    assert all(event.is_set() for event in events)
    assert all(handle.task.done() for handle in fork_runtime.handles.values())


@pytest.mark.asyncio
async def test_review_fork_usage_records_each_call_once_unattributed(review_env, monkeypatch):
    """(b): a review fork's provider calls reach the ledger, once per call key."""
    from core import fork_native as native
    from core.artifact_review_runtime import _run
    from core.fork_types import ProviderForkResult
    from infra.turn_usage_receipts import PLAN_FIELDS
    from plans import public
    from test_checkpoint_provider_adapters import _request

    db, aid = review_env
    row = request(db, aid)
    provider = replace(_request(None), fork_id=row["fork_id"], thread_id="reviewer")
    read_binding = []
    monkeypatch.setattr(public, "binding_at_call_start", lambda thread: read_binding.append(thread))
    measured = {
        "prompt_tokens": 100, "completion_tokens": 50, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "total_tokens": 150, "cost_usd": 0.25,
    }

    async def invoke(request_, _cancel, **_kwargs):
        await request_.on_usage("call-1", "native/test", measured)
        await request_.on_usage("call-1", "native/test", measured)
        await request_.on_usage("call-2", "native/test", measured)
        return ProviderForkResult("", None)

    async def busy_source(request_):
        return request_

    monkeypatch.setattr(native, "invoke_fork", invoke)
    monkeypatch.setattr("core.artifact_review_runtime._source", busy_source)
    await _run(row["id"], provider, asyncio.Event())

    rows = db.execute("SELECT * FROM llm_usage WHERE thread_id='reviewer'").fetchall()
    assert len(rows) == 2
    assert all(usage["total_tokens"] == 150 and usage["turn_id"] is None for usage in rows)
    assert all(usage[field] is None for usage in rows for field in PLAN_FIELDS)
    assert read_binding == []
    assert all(usage["source"] == "artifact_review" for usage in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind_name", "expected"), [
    ("checkpoint", "10 tokens over the remaining checkpoint room and was not sent; write the checkpoint now."),
    ("review", "10 tokens over the remaining review room and was not sent; "
               "leave your comments and call doc_feedback_send now."),
])
async def test_over_room_result_tells_each_kind_its_own_next_step(monkeypatch, kind_name, expected):
    """(c): the over-room reply names the kind's own finishing action."""
    from core import fork_native_tools
    from core.checkpoint_fork_kind import CHECKPOINT_KIND
    from llm.router import LLMResponse, ToolCallRequest
    from test_checkpoint_provider_adapters import _request

    base = CHECKPOINT_KIND if kind_name == "checkpoint" else ARTIFACT_REVIEW_KIND
    tool = sorted(base.tool_allowlist)[0]

    async def executor(*_args):
        return "x" * 50

    kind = replace(base, tool_executor=executor)
    fits = iter([True, False, True])

    async def input_fits(*_args):
        return next(fits)

    monkeypatch.setattr(fork_native_tools, "input_fits_off_loop", input_fits)
    monkeypatch.setattr(
        fork_native_tools, "input_room", lambda *_args: SimpleNamespace(counted=110, bound=100),
    )
    messages = []
    response = LLMResponse(content=None, tool_calls=[ToolCallRequest(id="big", name=tool, arguments={})])

    assert await fork_native_tools.run_tool_calls(
        SimpleNamespace(), response, messages, asyncio.Event(), _request(None), [], kind=kind,
    )
    assert messages[-1]["content"] == "Error: this result (50 characters) is " + expected


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [True, False])
async def test_warm_cc_review_advertises_the_source_tool_list(review_env, frozen):
    """B1: a warm fork keeps the source's tool bytes; the allowlist binds at execution."""
    from core.fork_cc_bridge import bind_bridge, definitions, execute
    from subagents.harness_tool_bridge import dynamic_tool_definitions
    from test_checkpoint_provider_adapters import _request

    db, aid = review_env
    row = request(db, aid)
    source_tools = ({"type": "function", "function": {"name": "write_file", "parameters": {}}},)
    warm = SimpleNamespace(tool_definitions=source_tools if frozen else None)
    provider = replace(_request(None), fork_id=row["fork_id"], thread_id="reviewer", warm_source=warm)
    with bind_bridge(provider, ARTIFACT_REVIEW_KIND, asyncio.Event()) as (_request_, handle):
        advertised = definitions(handle)
        expected = source_tools if frozen else dynamic_tool_definitions(handle.tool_registry)
        assert json.dumps(advertised, sort_keys=False) == json.dumps(expected, sort_keys=False)

        async def bound(operation, _call_id, _name, _arguments):
            return await operation()

        handle.tool_executor = bound
        result = await execute(handle, {"provider_tool_call_id": "call", "name": "write_file"})
        assert result == ARTIFACT_REVIEW_KIND.tool_refusal


@pytest.mark.asyncio
async def test_cc_warm_capture_carries_the_live_tool_list(tmp_path, monkeypatch):
    from core import fork_cc as cc
    from test_checkpoint_external_adapters import _boundary, _target
    from subagents.cc_invocation import ClaudeInvocation

    transcript = tmp_path / "source.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    seed_file, mcp_config = tmp_path / "seed.txt", tmp_path / "tools.json"
    seed_file.write_text("seed")
    mcp_config.write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude", permission_mode="bypassPermissions", session_id="source-session",
        seed_file=str(seed_file), mcp_config=str(mcp_config), working_dir=str(tmp_path),
        model="sonnet", effort="high",
    )
    live_tools = ({"type": "function", "function": {"name": "read_file"}},)

    async def live(_thread):
        return ("source-session", invocation, None, str(transcript), None, live_tools)

    monkeypatch.setattr(cc, "_live_identity", live)
    source = await cc.capture_warm_source(
        "thread-1", _boundary(), target=_target("claude-code"), binary="claude",
    )
    assert source is not None and source.tool_definitions == live_tools


def test_warm_codex_review_advertises_the_full_source_surface(tmp_path):
    """B1: Codex narrows only a cold fork; a warm one keeps the resumed surface."""
    from core.fork_codex import _fork_tools
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions
    from test_checkpoint_provider_adapters import _request

    cwd = str(tmp_path)
    registry = build_tool_registry(cwd)
    full = dynamic_tool_definitions(build_tool_registry(cwd))
    warm = replace(_request(None), warm_source=object(), tool_registry=registry)
    cold = replace(_request(None), warm_source=None, tool_registry=None)

    warm_registry, warm_tools = _fork_tools(warm, cwd, ARTIFACT_REVIEW_KIND)
    _cold_registry, cold_tools = _fork_tools(cold, cwd, ARTIFACT_REVIEW_KIND)

    assert warm_registry is registry
    assert json.dumps(warm_tools) == json.dumps(full)
    names = {(item.get("function") or item)["name"] for item in cold_tools}
    assert names and names <= ARTIFACT_REVIEW_KIND.tool_allowlist
