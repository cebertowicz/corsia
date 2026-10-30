"""A new top-level agent comment names itself on its ``doc_comment_changed`` frame.

A canvas needs the id and the author thread to hold the comment for its presence
lane before the reload that frame triggers can show it. User comments, replies,
suggestions and updates carry neither field.
"""

from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc  # noqa: F401
from test_doc_comment_change_frames import _user_comment
from test_doc_suggestions import _own, _suggest

pytestmark = pytest.mark.integration


@pytest.fixture
def sent(monkeypatch):
    """The messages the notifier sends, with the audience resolved and the socket stubbed."""
    from doceditor import comment_notifications

    messages: list[dict] = []
    monkeypatch.setattr(comment_notifications, "_comment_change_audience", lambda _id, db=None: "all")
    monkeypatch.setattr(comment_notifications, "_send", lambda _audience, message: messages.append(message))
    return messages


@pytest.mark.asyncio
async def test_an_agent_create_names_the_comment_and_its_author_thread(
    integration_client, doc, _registrable_roots, sent,  # noqa: F811
):
    from tools.documents import DocCommentCreateTool

    _owner, artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    created = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="Design", body="Check this.", _thread_id=reviewer,
    ))
    assert sent == [{
        "type": "doc_comment_changed", "artifact_id": artifact_id,
        "comment_id": created["comment"]["id"], "author_thread_id": reviewer,
    }]


@pytest.mark.asyncio
async def test_user_comments_replies_suggestions_and_updates_name_nothing(
    integration_client, doc, _registrable_roots, sent,  # noqa: F811
):
    from tools.documents import DocCommentCreateTool, DocCommentUpdateTool

    owner, artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    plain = {"type": "doc_comment_changed", "artifact_id": artifact_id}

    user = _user_comment(integration_client, artifact_id)
    assert user.status_code == 200, user.text
    assert sent == [plain]

    del sent[:]
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="Design", body="Root.", _thread_id=reviewer,
    ))["comment"]
    del sent[:]
    reply = json.loads(await DocCommentCreateTool().execute(
        body="Reply.", parent_id=root["id"], _thread_id=reviewer,
    ))
    assert reply["comment"]["parent_id"] == root["id"]
    assert sent == [{**plain, "reply_attention_changed": True}]

    del sent[:]
    await _suggest(reviewer, doc, replacement="Another tail sentence")
    assert sent == [plain]

    del sent[:]
    updated = json.loads(await DocCommentUpdateTool().execute(
        comment_id=root["id"], acked=True, _thread_id=owner,
    ))
    assert updated["comment"]["acked"] == 1
    assert sent == [plain]
