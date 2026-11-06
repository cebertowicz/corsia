"""Review listings use the model actually reading Local-only comments."""

import asyncio
from types import SimpleNamespace

import pytest

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from doc_feedback_helpers import add_comment
from test_doc_review_requests import request, review_env  # noqa: F401
from tools.documents import DocCommentsListTool


@pytest.mark.asyncio
@pytest.mark.parametrize("model,visible", [("cloud/model", False), ("local/model", True), (None, False)])
async def test_fork_listing_uses_frozen_model_and_fails_closed(review_env, monkeypatch, model, visible):
    from infra import local_only

    db, aid = review_env
    row = request(db, aid)
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.execute("UPDATE threads SET model='local/model', pending_model=NULL WHERE id='reviewer'")
    db.commit()
    comment, _ = add_comment(db, aid, author_id="second")
    monkeypatch.setattr(local_only, "_is_cloud", lambda value, **_kw: value != "local/model")
    result = await DocCommentsListTool().execute(
        artifact_id=aid, _thread_id="reviewer", _review_reader_model=model,
    )
    assert (comment["id"] in result) is visible


@pytest.mark.asyncio
async def test_cloud_fork_withholds_local_reply_to_cloud_root(review_env, monkeypatch):
    from infra import local_only

    db, aid = review_env
    request(db, aid)
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.commit()
    root, _ = add_comment(db, aid, author_id='owner')
    reply, _ = add_comment(db, aid, author_id='second', parent_id=root['id'])
    monkeypatch.setattr(local_only, '_is_cloud', lambda *_args, **_kw: True)
    result = await DocCommentsListTool().execute(
        artifact_id=aid, _thread_id='reviewer', _review_reader_model='cloud/model',
    )
    assert root['id'] in result
    assert reply['id'] not in result


@pytest.mark.asyncio
async def test_cloud_fork_keeps_public_replies_to_withheld_root(review_env, monkeypatch):
    from infra import local_only

    db, aid = review_env
    row = request(db, aid)
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.commit()
    root, _ = add_comment(db, aid, author_id="second")
    reply, _ = add_comment(db, aid, author_id="owner", parent_id=root["id"])
    db.execute("UPDATE doc_comments SET body='Local secret' WHERE id=?", (root["id"],))
    db.execute("UPDATE doc_comments SET body='Public reply' WHERE id=?", (reply["id"],))
    db.commit()
    monkeypatch.setattr(local_only, "_is_cloud", lambda *_args, **_kw: True)
    result = await DocCommentsListTool().execute(
        artifact_id=aid, _thread_id="reviewer", _review_reader_model="cloud/model",
    )
    assert "Local secret" not in result
    assert "Public reply" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(('resolved', 'decision'), [(1, None), (0, 'accepted')])
async def test_withheld_root_preserves_closed_state(review_env, monkeypatch, resolved, decision):
    from doceditor.comment_anchoring import anchored_threads
    from doceditor.comment_recipients import visible_comment_threads
    from doceditor.suggestion_overlap import is_open_thread
    from tools.document_comment_listing import select_comment_threads
    from infra import local_only

    db, aid = review_env
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.commit()
    root, _ = add_comment(db, aid, author_id='second')
    add_comment(db, aid, author_id='owner', parent_id=root['id'])
    db.execute('UPDATE doc_comments SET resolved=?, decision=? WHERE id=?', (resolved, decision, root['id']))
    db.commit()
    monkeypatch.setattr(local_only, '_is_cloud', lambda *_args, **_kw: True)
    threads, _ = anchored_threads(aid, None, db, text='')
    threads = visible_comment_threads(db, threads, 'reviewer', reader={'model': 'cloud/model', 'pending_model': None})
    assert len(threads) == 1 and not is_open_thread(threads[0])
    assert select_comment_threads(threads, 'open', None)['closed_count'] == 1


def test_ordinary_listing_visibility_uses_one_query_without_local_only_authors(review_env):
    from doceditor.comment_recipients import visible_comment_threads

    db, aid = review_env
    root, _ = add_comment(db, aid, author_id='second')
    statements = []
    db.set_trace_callback(statements.append)
    try:
        assert visible_comment_threads(db, [root], 'reviewer') == [{**root, 'replies': []}]
    finally:
        db.set_trace_callback(None)
    assert sum(sql.lstrip().upper().startswith('SELECT') for sql in statements) == 1


def test_withheld_suggestion_preserves_anchor_without_revealing_replacement(review_env, monkeypatch):
    from doceditor.comment_recipients import visible_comment_threads
    from infra import local_only

    db, _aid = review_env
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.commit()
    monkeypatch.setattr(local_only, '_is_cloud', lambda *_args, **_kw: True)
    root = {'id': 'root', 'parent_id': None, 'author_kind': 'agent', 'author_thread_id': 'second',
            'author': 'Private', 'body': 'Secret body', 'review': False, 'anchor_text': 'quoted text',
            'replacement': 'Secret replacement', 'created_at': 'today', 'replies': [
                {'id': 'reply', 'author_kind': 'agent', 'author_thread_id': 'owner', 'body': 'Public'}]}
    visible = visible_comment_threads(db, [root], 'reviewer', reader={'model': 'cloud/model', 'pending_model': None})[0]
    assert visible['review'] is False and visible['anchor_text'] == 'quoted text'
    assert visible['created_at'] == 'today'
    assert 'Secret' not in str(visible)
    assert visible['replacement'] == '(not shown: this agent is Local only)'


@pytest.mark.asyncio
async def test_fork_clears_parent_tool_invocation(review_env):
    from core.fork_tools import execute_fork_tool
    from infra.state.context import running_call_ids, tool_invocation_context

    db, aid = review_env
    row = request(db, aid)
    seen = []

    async def execute(*_args, **_kwargs):
        seen.append(running_call_ids())
        return "ok"

    provider = SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer", working_dir="/workspace",
                               target=SimpleNamespace(catalog_id='cloud/model'))
    with tool_invocation_context("parent-call"):
        assert await execute_fork_tool(ARTIFACT_REVIEW_KIND, provider, SimpleNamespace(execute=execute),
                                       asyncio.Event(), "read_file", {"path": "doc.md"}) == "ok"
        assert running_call_ids()["tool_invocation_id"] == "parent-call"
    assert seen == [{}]


@pytest.mark.asyncio
async def test_ordinary_listing_keeps_local_reader_comments(review_env, monkeypatch):
    from infra import local_only

    db, aid = review_env
    db.execute("UPDATE threads SET local_only=1 WHERE id='second'")
    db.execute("UPDATE threads SET model='local/model', pending_model=NULL WHERE id='reviewer'")
    db.commit()
    comment, _ = add_comment(db, aid, author_id="second")
    monkeypatch.setattr(local_only, "_is_cloud", lambda value, **_kw: value != "local/model")
    result = await DocCommentsListTool().execute(artifact_id=aid, _thread_id="reviewer")
    assert comment["id"] in result
