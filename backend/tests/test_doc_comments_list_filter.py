"""Agent comment listing keeps open threads accessible and closed threads retrievable."""

import json

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc

pytestmark = pytest.mark.integration


@pytest.fixture
async def comment_threads(integration_client, doc, _registrable_roots):
    from doceditor import comments as store
    from tools.documents import DocCommentCreateTool

    thread_id = _mk_thread(integration_client, _OWNER, title="Reviewer")
    created = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Open thread", _thread_id=thread_id,
    ))["comment"]
    aid = created["artifact_id"]
    reply = store.create(aid, body="Open reply", parent_id=created["id"], announce=False)
    orphan = store.create(aid, body="Orphan", anchor_text="A quote that is missing",
                          from_off=0, to_off=23, announce=False)
    resolved = store.create(aid, body="Resolved", announce=False)
    resolved_reply = store.create(aid, body="Resolved reply", parent_id=resolved["id"], announce=False)
    accepted = store.create(aid, body="Accepted", announce=False)
    rejected = store.create(aid, body="Rejected", announce=False)
    store.update(resolved["id"], {"resolved": True})
    store.update(accepted["id"], {"decision": "accepted"})
    store.update(rejected["id"], {"decision": "rejected"})
    return thread_id, aid, created, reply, orphan, resolved, resolved_reply, accepted, rejected


@pytest.mark.asyncio
async def test_default_open_threads_keep_replies_and_orphans(comment_threads):
    from tools.documents import DocCommentsListTool

    sid, aid, opened, reply, orphan, *_ = comment_threads
    result = json.loads(await DocCommentsListTool().execute(artifact_id=aid, _thread_id=sid))
    assert result["found"] is True
    assert result["artifact_id"] == aid
    assert result["anchors_checked"] is True
    assert result["closed_count"] == 3
    assert result["note"] == "3 resolved or decided comment threads not shown; pass status='all' to include them."
    rows = {row["id"]: row for row in result["comments"]}
    assert set(rows) == {opened["id"], orphan["id"]}
    assert [row["id"] for row in rows[opened["id"]]["replies"]] == [reply["id"]]
    assert rows[orphan["id"]]["orphaned"] is True
    all_rows = json.loads(await DocCommentsListTool().execute(
        artifact_id=aid, status="all", _thread_id=sid,
    ))["comments"]
    assert rows[opened["id"]] == next(row for row in all_rows if row["id"] == opened["id"])


@pytest.mark.asyncio
async def test_all_includes_every_thread(comment_threads):
    from tools.documents import DocCommentsListTool

    sid, aid, opened, _, orphan, resolved, _, accepted, rejected = comment_threads
    result = json.loads(await DocCommentsListTool().execute(artifact_id=aid, status="all", _thread_id=sid))
    assert {row["id"] for row in result["comments"]} == {
        opened["id"], orphan["id"], resolved["id"], accepted["id"], rejected["id"],
    }
    assert result["closed_count"] == 0
    assert "note" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_reply", [False, True])
async def test_comment_id_returns_closed_thread_with_all_replies(comment_threads, lookup_reply):
    from tools.documents import DocCommentsListTool

    sid, aid, _, _, _, resolved, reply, *_ = comment_threads
    comment_id = reply["id"] if lookup_reply else resolved["id"]
    result = json.loads(await DocCommentsListTool().execute(
        artifact_id=aid, status="open", comment_id=comment_id, _thread_id=sid,
    ))
    assert result["closed_count"] == 0
    assert len(result["comments"]) == 1
    assert result["comments"][0]["id"] == resolved["id"]
    assert [row["id"] for row in result["comments"][0]["replies"]] == [reply["id"]]
    assert "note" not in result


@pytest.mark.asyncio
async def test_unknown_comment_id(comment_threads):
    from tools.documents import DocCommentsListTool

    sid, aid, *_ = comment_threads
    result = json.loads(await DocCommentsListTool().execute(
        artifact_id=aid, comment_id="missing", _thread_id=sid,
    ))
    assert result["found"] is True
    assert result["comments"] == []
    assert result["closed_count"] == 0
    assert result["note"] == "No comment with that id on this document."
    assert "error" not in result


@pytest.mark.asyncio
async def test_bad_status(comment_threads):
    from tools.documents import DocCommentsListTool

    sid, aid, *_ = comment_threads
    assert await DocCommentsListTool().execute(artifact_id=aid, status="closed", _thread_id=sid) == (
        "Error: status must be 'open' or 'all'"
    )


def test_listing_schema():
    from tools.documents import DocCommentsListTool

    props = DocCommentsListTool().parameters["properties"]
    assert props["status"] == {"type": "string", "enum": ["open", "all"], "default": "open"}
    assert props["comment_id"]["type"] == ["string", "null"]
