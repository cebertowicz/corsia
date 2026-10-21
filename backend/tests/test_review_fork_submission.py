"""Fork sends and receipts follow ordinary reviewer feedback delivery."""

import asyncio
from types import SimpleNamespace

import pytest

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_tools import execute_fork_tool
from doceditor import comments, review_feedback
from doceditor.feedback_batches import claim_and_deliver
from infra.database import write_transaction
from test_doc_review_requests import request, review_env, fork_comment, stored  # noqa: F401


@pytest.mark.parametrize("deleted_comments,pending", [(0, False), (1, True)])
def test_empty_submit_finishes_once_without_feedback_or_reviewer_wake(
    review_env, deleted_comments, pending,
):
    db, aid = review_env
    row = request(db, aid)
    for _ in range(deleted_comments):
        fork_comment(db, aid, row["id"])
    with write_transaction(db, scope="test_empty_fork_submit"):
        db.execute("DELETE FROM doc_comments")
        assert review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid) == (pending, [], [])
    assert stored(db, row["id"])["state"] == "submitted"
    with write_transaction(db, scope="test_repeated_empty_fork_submit"):
        assert review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid) == (False, [], [])
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_owner_batch_delivery_does_not_end_a_running_fork(review_env):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    from test_review_feedback_ownership import ordinary_comment
    batch = ordinary_comment(db, aid)
    with write_transaction(db, scope="test_delivery_before_fork_submit"):
        claim_and_deliver(db, batch, from_state="open", reason="submit")
    assert stored(db, row["id"])["state"] == "forked"
    fork_comment(db, aid, row["id"])
    with write_transaction(db, scope="test_fork_ends_after_delivery"):
        review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    assert stored(db, row["id"])["state"] == "submitted"
    assert db.execute("SELECT item_count FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()[0] == 2


def test_duplicate_comment_with_submit_delivers_existing_ordinary_feedback(review_env):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    _comment, _registered, result = fork_comment(db, aid, row["id"], submit=True)
    assert set(result) == {"batch_ids", "undelivered_batches", "woken"}
    assert not result["undelivered_batches"]
    assert stored(db, row["id"])["state"] == "submitted"
    with write_transaction(db, scope="test_repeated_duplicate_fork_submit"):
        assert review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid) == (False, [], [])
    assert stored(db, row["id"])["state"] == "submitted"
    assert db.execute("SELECT COUNT(*) FROM doc_comments").fetchone()[0] == 1


def test_submit_comment_finishes_a_fork_without_a_continuation_or_reviewer_wake(review_env):
    from infra.passive_changes import materialize_pending_digest

    db, aid = review_env
    row = request(db, aid)

    _comment, _registered, feedback = fork_comment(db, aid, row["id"], submit=True)

    assert stored(db, row["id"])["state"] == "submitted"
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE owner_thread_id='reviewer'").fetchone()[0] == 0
    assert "reviewer" not in feedback["woken"]
    assert materialize_pending_digest(db, "reviewer") is not None


@pytest.mark.parametrize("root_kind,root_author,recipient", [
    ("user", None, "ui"), ("agent", "second", "second"),
])
def test_fork_reply_reaches_root_author_through_ordinary_routing(review_env, root_kind, root_author, recipient):
    from doceditor.comment_create_db import CommentInput, create_in_db
    from doceditor.comment_creation import CreateReceipt

    db, aid = review_env
    root = comments.create(
        aid, body="Which claim needs evidence?", author="Reader", author_kind=root_kind,
        author_thread_id=root_author, review=True, db=db,
    )
    row = request(db, aid)
    reply, _registered, feedback = create_in_db(
        CommentInput(
            artifact_id=aid, path=None, body="The delivery claim.", author="Reviewer",
            author_kind="agent", user_id="owner-token", author_thread_id="reviewer",
            parent_id=root["id"], review_request_id=row["id"],
        ), None, f"reply-{root_kind}", CreateReceipt(),
    )
    assert len(feedback["batch_ids"]) == 2
    with write_transaction(db, scope="test_send_fork_reply"):
        review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    batches = db.execute("SELECT id,recipient_id,state,delivered_node_id FROM doc_feedback_batches").fetchall()
    assert {batch["recipient_id"] for batch in batches} == {"owner", recipient}
    assert all(batch["state"] == "delivered" for batch in batches)
    recipient_batch = next(batch for batch in batches if batch["recipient_id"] == recipient)
    assert db.execute(
        "SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=?", (recipient_batch["id"],),
    ).fetchone()[0] == reply["id"]
    if root_kind == "agent":
        node = db.execute("SELECT description FROM plan_nodes WHERE id=?", (recipient_batch["delivered_node_id"],)).fetchone()
        assert "The delivery claim." in node["description"]
    assert stored(db, row["id"])["state"] == "submitted"


def test_empty_fork_sends_reviewers_existing_batch_and_receipt_counts_it(review_env):
    from test_review_feedback_ownership import ordinary_comment

    db, aid = review_env
    row = request(db, aid)
    ordinary_comment(db, aid)
    with write_transaction(db, scope="test_fork_sends_reviewer_batch"):
        pending, _woken, failures = review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    assert pending and not failures
    assert stored(db, row["id"])["state"] == "submitted"
    receipt = db.execute(
        "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' "
        "AND object_id='reviewer' ORDER BY created_at DESC LIMIT 1",
    ).fetchone()["summary"]
    assert "1 review comments written; feedback was delivered" in receipt
    assert "The continuation adds evidence." in receipt
    assert "nothing was sent to the document owner" not in receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("read_file", {"path": "/unrelated.md"}),
    ("doc_comments_list", {"artifact_id": "unrelated"}),
    ("doc_comment_create", {"artifact_id": "unrelated", "body": "Out of scope"}),
    ("doc_feedback_send", {"artifact_id": "unrelated"}),
])
async def test_out_of_scope_arguments_return_kind_refusal_before_dispatch(review_env, name, arguments):
    db, aid = review_env
    row = request(db, aid)
    calls = []

    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return "unexpected"

    result = await execute_fork_tool(
        ARTIFACT_REVIEW_KIND,
        SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir="/workspace"),
        SimpleNamespace(execute=execute), asyncio.Event(), name, arguments,
    )
    assert result == ARTIFACT_REVIEW_KIND.tool_refusal
    assert calls == []


@pytest.mark.asyncio
async def test_valid_document_path_cannot_hide_an_unrelated_artifact(review_env):
    db, aid = review_env
    row = request(db, aid)
    path = db.execute("SELECT path FROM artifact_identities WHERE id=?", (aid,)).fetchone()[0]

    async def execute(*args, **kwargs):
        raise AssertionError("the conflicting artifact must be refused before dispatch")

    result = await execute_fork_tool(
        ARTIFACT_REVIEW_KIND,
        SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir="/workspace"),
        SimpleNamespace(execute=execute), asyncio.Event(), "doc_comment_create",
        {"doc_path": path, "artifact_id": "unrelated", "body": "Out of scope"},
    )
    assert result == ARTIFACT_REVIEW_KIND.tool_refusal


@pytest.mark.asyncio
async def test_codex_bridge_returns_scope_refusal_without_a_foreground_turn(review_env):
    from core.fork_tools import ForkToolBoundary
    from subagents.harness_tool_bridge import execute_tool
    from tools.registry import ToolRegistry

    db, aid = review_env
    row = request(db, aid)
    registry = ToolRegistry()
    provider = SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir="/workspace")
    policy = ForkToolBoundary(ARTIFACT_REVIEW_KIND, provider, registry, asyncio.Event(), None)
    result = await execute_tool(
        registry, thread_id="reviewer", fork_id=row["fork_id"], fork_policy=policy,
        name="doc_feedback_send", arguments={"artifact_id": "unrelated"}, harness="codex",
    )
    assert result == ARTIFACT_REVIEW_KIND.tool_refusal
    assert stored(db, row["id"])["state"] == "forked"
    assert db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id='reviewer'").fetchone()[0] == 0


def test_codex_fork_identity_alone_cannot_bypass_launch_tool_policy():
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_app_server_errors import CodexWriteSurfaceError

    session = CodexAppServerSession(binary="unused", task="Review", fork_id="fork-1")
    with pytest.raises(CodexWriteSurfaceError):
        session._assert_launch_write_surface()


def test_codex_launch_accepts_the_actual_review_tool_boundary():
    from core.fork_tools import ForkToolBoundary
    from subagents.codex_app_server import CodexAppServerSession
    from tools.registry import ToolRegistry

    policy = ForkToolBoundary(
        ARTIFACT_REVIEW_KIND, SimpleNamespace(fork_id="fork-1", thread_id="reviewer"),
        ToolRegistry(), asyncio.Event(), None,
    )
    session = CodexAppServerSession(
        binary="unused", task="Review", fork_id="fork-1", fork_tool_result_fits=policy,
    )
    session._assert_launch_write_surface()


@pytest.mark.asyncio
async def test_registry_strips_forged_review_scope_and_injects_only_trusted_scope(review_env):
    from infra import state
    from test_tool_registry_identity import _CaptureTool
    from tools.registry import ToolRegistry

    registry, tool = ToolRegistry(), _CaptureTool()
    registry.register(tool)
    token = state._current_thread_id.set("reviewer")
    try:
        forged = {"value": "safe", "_review_request_id": "forged"}
        assert await registry.execute("capture", forged, review_request_id="trusted") == "ok"
        assert await registry.execute("capture", forged) == "ok"
    finally:
        state._current_thread_id.reset(token)
    assert tool.calls[0]["_review_request_id"] == "trusted"
    assert "_review_request_id" not in tool.calls[1]


@pytest.mark.asyncio
async def test_executor_passes_request_scope_explicitly(review_env):
    from doceditor.comment_create_db import CommentInput

    db, aid = review_env
    row = request(db, aid)
    calls = []

    async def execute(name, arguments, **scope):
        calls.append(scope)
        return "ok"

    result = await execute_fork_tool(
        ARTIFACT_REVIEW_KIND,
        SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir="/workspace"),
        SimpleNamespace(execute=execute), asyncio.Event(), "doc_comments_list", {"artifact_id": aid},
    )
    assert result == "ok"
    assert calls == [{"review_request_id": row["id"]}]
    assert CommentInput.__dataclass_fields__["review_request_id"].default is None
