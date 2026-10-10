"""Who may resolve or acknowledge a comment as an agent (DCOL-FR10.4): the
document's owner or the comment's author, through the native tool and the
agent route the ``corsia doc`` CLI calls. Anyone else replies."""

from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _patch, _registrable_roots, doc

pytestmark = pytest.mark.integration

QUOTE = "A unique tail sentence"


async def _setup(client, doc):
    """An owner thread registers ``doc``; an author thread comments; a third thread looks on."""
    from tools.artifacts import ArtifactRegisterTool
    from tools.documents import DocCommentCreateTool

    owner = _mk_thread(client, _OWNER, title="Doc Owner")
    await ArtifactRegisterTool().execute(path=str(doc), _thread_id=owner)
    author = _mk_thread(client, _OWNER, title="Reviewer")
    created = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=QUOTE, body="Tighten this.", _thread_id=author,
    )
    bystander = _mk_thread(client, _OWNER, title="Bystander")
    return owner, author, bystander, json.loads(created)["comment"]["id"]


async def _tool_update(thread_id, comment_id, **fields):
    from tools.documents import DocCommentUpdateTool

    return await DocCommentUpdateTool().execute(comment_id=comment_id, _thread_id=thread_id, **fields)


def _row(comment_id):
    from doceditor import comments as store

    return store.get_comment(comment_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["resolved", "acked"])
async def test_bystander_tool_is_refused_with_reply_as_next_step(integration_client, doc, _registrable_roots, field):
    _owner, _author, bystander, cid = await _setup(integration_client, doc)
    result = await _tool_update(bystander, cid, **{field: True})
    assert result.startswith("Error: Only the document's owner (Doc Owner) or the comment's author")
    assert "Reply to the comment instead" in result
    assert _row(cid)[field] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["resolved", "acked"])
async def test_bystander_route_is_refused(integration_client, doc, _registrable_roots, field):
    _owner, _author, bystander, cid = await _setup(integration_client, doc)
    r = _patch(integration_client, _OWNER, cid, as_session_id=bystander, **{field: True})
    assert r.status_code == 403
    assert "Reply to the comment instead" in r.json()["detail"]
    assert _row(cid)[field] == 0


@pytest.mark.asyncio
async def test_owner_resolves_and_acks_another_agents_comment(integration_client, doc, _registrable_roots):
    owner, _author, _bystander, cid = await _setup(integration_client, doc)
    acked = json.loads(await _tool_update(owner, cid, acked=True))["comment"]
    assert acked["acked"] == 1
    r = _patch(integration_client, _OWNER, cid, resolved=True, as_session_id=owner)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["resolved"] == 1


@pytest.mark.asyncio
async def test_author_resolves_and_reopens_own_comment_on_anothers_document(integration_client, doc, _registrable_roots):
    _owner, author, _bystander, cid = await _setup(integration_client, doc)
    resolved = json.loads(await _tool_update(author, cid, resolved=True))["comment"]
    assert resolved["resolved"] == 1
    r = _patch(integration_client, _OWNER, cid, resolved=False, as_session_id=author)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["resolved"] == 0


@pytest.mark.asyncio
async def test_owner_still_cannot_edit_another_agents_comment_text(integration_client, doc, _registrable_roots):
    owner, _author, _bystander, cid = await _setup(integration_client, doc)
    result = await _tool_update(owner, cid, body="Owner rewrites it.")
    assert result.startswith("Error: an agent may only edit the content of comments it authored")
    assert _row(cid)["body"] == "Tighten this."
