"""Ordinary multi-comment reviews fit the same job budget on all three harnesses."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND, REVIEW_STEP_BUDGET
from core.fork_tools import ForkToolBoundary
from test_doc_review_requests import request, review_env, stored
from test_checkpoint_provider_adapters import _request


def review_tools(tmp_path):
    from tools.registry import ToolRegistry
    from tools.documents import DocCommentCreateTool, DocCommentsListTool
    from tools.document_feedback import DocFeedbackSendTool
    from tools.filesystem_rw import ReadFileTool

    registry = ToolRegistry()
    for tool in (ReadFileTool(workspace=tmp_path), DocCommentsListTool(), DocCommentCreateTool(), DocFeedbackSendTool()):
        registry.register(tool)
    return registry


async def native_review(provider, registry, calls, cancel, request_id, monkeypatch):
    from core import checkpoint_native_fork as native
    from core.artifact_review_runtime import _run
    from llm.router import LLMResponse, ToolCallRequest

    responses = iter([
        *(LLMResponse(content="I will check this point.", tool_calls=[
            ToolCallRequest(id=f"call-{index}", name=name, arguments=arguments),
        ]) for index, (name, arguments) in enumerate(calls)),
        LLMResponse(content="Review submitted."),
    ])

    async def agent():
        return SimpleNamespace(max_iterations=3, tools=registry)

    async def complete(**_kwargs):
        return next(responses)

    async def busy_source(request_):
        return request_

    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_provider_params", lambda *_args: {})
    monkeypatch.setattr(native, "_chat_completion", complete)
    monkeypatch.setattr("core.artifact_review_runtime._source", busy_source)
    await _run(request_id, provider, cancel)


async def cc_review(provider, calls, cancel):
    from core.fork_cc_bridge import bind_bridge, execute
    from core.checkpoint_cc_commands import cold_command
    from core.checkpoint_cc_result import result

    command = cold_command(replace(provider, binary="claude"), ARTIFACT_REVIEW_KIND)
    limit = int(command[command.index("--max-turns") + 1])
    assert limit == REVIEW_STEP_BUDGET
    with bind_bridge(provider, ARTIFACT_REVIEW_KIND, cancel) as (_configured, handle):
        async def bound(operation, _call_id, _name, _arguments):
            return await operation()

        handle.tool_executor = bound
        for turn, (name, arguments) in enumerate(calls, 1):
            assert turn < limit
            response = await execute(handle, {"provider_tool_call_id": f"tool-{turn}",
                                              "name": name, "arguments": arguments})
            assert not str(response).startswith("Error:")
    assert result(b'{"type":"result","result":"Review submitted.","num_turns":7}',
                  kind=ARTIFACT_REVIEW_KIND).raw_body == "Review submitted."


async def codex_review(provider, registry, calls, cancel):
    from core.fork_codex_control import CompletedItemLimit

    killed = []
    session = SimpleNamespace(
        _is_active_provider_boundary=lambda thread, turn: (thread, turn) == ("owned", "turn"),
        revoke_dynamic_tool_admission=lambda: killed.append("revoke"),
        _request_terminate=lambda: killed.append("terminate"),
    )
    limit = CompletedItemLimit(ARTIFACT_REVIEW_KIND, provider)
    boundary = ForkToolBoundary(ARTIFACT_REVIEW_KIND, provider, registry, cancel, None)
    for index, (name, arguments) in enumerate(calls):
        for item_type in ("reasoning", "agentMessage"):
            await limit.observe(session, {"method": "item/completed", "params": {
                "threadId": "owned", "turnId": "turn",
                "item": {"id": f"{item_type}-{index}", "type": item_type},
            }})
        response = await boundary.execute(name, arguments)
        assert not str(response).startswith("Error:")
        await limit.observe(session, {"method": "item/completed", "params": {
            "threadId": "owned", "turnId": "turn",
            "item": {"id": f"tool-{index}", "type": "dynamicToolCall"},
        }})
    assert len(limit.seen) == len(calls) and not limit.reached.is_set()
    assert not killed


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "claude-code", "codex"])
async def test_ordinary_review_submits_before_shared_budget(review_env, tmp_path, monkeypatch, harness):
    db, aid = review_env
    path = tmp_path / "review.md"
    path.write_text("# Proposal\n\nThree claims need evidence.\n")
    db.execute("UPDATE artifact_identities SET path=? WHERE id=?", (str(path), aid))
    db.commit()
    row = request(db, aid)
    registry = review_tools(tmp_path)
    provider = replace(_request(None), fork_id=row["fork_id"], thread_id="reviewer",
                       working_dir=str(tmp_path), tool_registry=registry)
    calls = [
        ("doc_comments_list", {"artifact_id": aid}),
        ("read_file", {"path": str(path)}),
        *(("doc_comment_create", {"artifact_id": aid, "body": f"Claim {number} needs evidence.",
                                 "review": True, "client_id": f"claim-{number}"}) for number in range(3)),
        ("doc_feedback_send", {"artifact_id": aid}),
    ]
    assert len(calls) + 1 < REVIEW_STEP_BUDGET
    cancel = asyncio.Event()
    if harness == "native":
        await native_review(provider, registry, calls, cancel, row["id"], monkeypatch)
    elif harness == "claude-code":
        await cc_review(provider, calls, cancel)
    else:
        await codex_review(provider, registry, calls, cancel)
    assert stored(db, row["id"])["state"] == "submitted"
    assert db.execute("SELECT COUNT(*) FROM doc_comments WHERE artifact_id=?", (aid,)).fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE owner_thread_id='reviewer'").fetchone()[0] == 0


def test_cc_limit_and_provider_failures_are_not_review_verdicts():
    from core.checkpoint_cc_result import result
    from core.artifact_review_capture import failure_reason
    from core.fork_runtime import ForkStepLimitReached
    from core.fork_types import ProviderForkRejected

    with pytest.raises(ForkStepLimitReached):
        result(b'{"type":"result","subtype":"error_max_turns","is_error":true}', kind=ARTIFACT_REVIEW_KIND)
    with pytest.raises(ProviderForkRejected) as error:
        result(b'{"type":"result","is_error":true,"result":"Missing credentials"}', kind=ARTIFACT_REVIEW_KIND)
    assert failure_reason(error.value) == "provider_error:ProviderForkRejected"
