"""A checkpoint rebuild updates the reader model before a fork invokes tools."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core import artifact_review_runtime as runtime, fork_runtime
from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from doc_feedback_helpers import add_comment
from test_doc_review_requests import request, review_env  # noqa: F401
from tools.documents import DocCommentsListTool


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "codex"])
async def test_rebuilt_fork_uses_rebuilt_model_for_listing(review_env, monkeypatch, harness):
    from infra import local_only
    from infra import checkpoint_queries
    from core import checkpoint_runtime

    db, aid = review_env
    row = request(db, aid)
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.commit()
    comment, _ = add_comment(db, aid, author_id="second")
    monkeypatch.setattr(local_only, "_is_cloud", lambda value, **_kw: value != "local/model")
    old = SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer",
                          target=SimpleNamespace(catalog_id="local/model", harness=harness))
    rebuilt = SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer",
                              target=SimpleNamespace(catalog_id="cloud/model", harness=harness))
    handle = fork_runtime.ForkHandle(asyncio.current_task(), asyncio.Event(), "reviewer",
                                     ARTIFACT_REVIEW_KIND, request=old)
    fork_runtime.handles[row["fork_id"]] = handle
    monkeypatch.setattr(runtime, "fork_ended", lambda *_: False)
    monkeypatch.setattr(runtime, "_cold_request", lambda *_: (row, rebuilt))
    monkeypatch.setattr(checkpoint_queries, "active_for_thread", lambda *_: SimpleNamespace(id="checkpoint"))

    async def finished(*_args):
        return None

    async def identity(value):
        return value

    invoked = []

    async def invoke(actual, *_args, **_kwargs):
        assert actual is rebuilt and handle.request is old
        async def execute(name, arguments, *, review_request_id, review_reader_model):
            assert name == 'doc_comments_list' and review_request_id == row['id']
            return await DocCommentsListTool().execute(**arguments, _thread_id='reviewer',
                                                       _review_reader_model=review_reader_model)
        actual.working_dir = '/workspace'
        result = await ARTIFACT_REVIEW_KIND.tool_executor(
            SimpleNamespace(tools=SimpleNamespace(execute=execute)), 'doc_comments_list',
            {'artifact_id': aid}, asyncio.Event(), actual,
        )
        assert comment["id"] not in result
        invoked.append(True)
        return "done"

    monkeypatch.setattr(checkpoint_runtime, "await_checkpoint", finished)
    monkeypatch.setattr(runtime, "_source", identity)
    monkeypatch.setattr(fork_runtime, "invoke_fork", invoke)
    monkeypatch.setattr(runtime, "ARTIFACT_REVIEW_KIND", replace(
        ARTIFACT_REVIEW_KIND, usage_binder=identity, end_handler=finished,
    ))
    try:
        await runtime._review(row["id"], old, asyncio.Event())
        assert invoked == [True]
    finally:
        fork_runtime.handles.pop(row["fork_id"], None)
