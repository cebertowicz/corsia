"""DCOL-FR10.4: an agent author hears, as a passive note, when someone else resolves, acknowledges or re-opens its comment."""

from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import _OWNER, _patch, _registrable_roots, doc  # noqa: F401
from test_doc_comment_resolve_rights import _setup, _tool_update

pytestmark = pytest.mark.integration


def _notes(thread_id):
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=? "
            "ORDER BY seq",
            (thread_id,),
        ).fetchall()
    finally:
        db.close()
    return [row["summary"] for row in rows]


def _user_patch(client, comment_id, **fields):
    return client.patch(f"/api/comments/{comment_id}?token={_OWNER}", json=fields)


@pytest.mark.asyncio
async def test_the_owner_tool_resolving_and_reopening_notes_the_author(integration_client, doc, _registrable_roots):
    owner, author, _bystander, cid = await _setup(integration_client, doc)

    await _tool_update(owner, cid, resolved=True)
    await _tool_update(owner, cid, resolved=False)

    assert _notes(author) == [
        f"Doc Owner resolved your comment on {doc.stem}.",
        f"Doc Owner re-opened your comment on {doc.stem}.",
    ]


@pytest.mark.asyncio
async def test_the_owner_tool_acknowledging_notes_the_author_once(integration_client, doc, _registrable_roots):
    owner, author, _bystander, cid = await _setup(integration_client, doc)

    await _tool_update(owner, cid, acked=True)
    await _tool_update(owner, cid, acked=True)

    assert _notes(author) == [f"Doc Owner acknowledged your comment on {doc.stem}."]


@pytest.mark.asyncio
async def test_the_agent_route_notes_the_author(integration_client, doc, _registrable_roots):
    owner, author, _bystander, cid = await _setup(integration_client, doc)

    assert _patch(integration_client, _OWNER, cid, resolved=True, as_session_id=owner).status_code == 200

    assert _notes(author) == [f"Doc Owner resolved your comment on {doc.stem}."]


@pytest.mark.asyncio
async def test_the_user_route_notes_the_author_of_a_resolve_but_not_a_reopen_that_already_queues_feedback(
    integration_client, doc, _registrable_roots,
):
    _owner, author, _bystander, cid = await _setup(integration_client, doc)

    assert _user_patch(integration_client, cid, resolved=True).status_code == 200
    assert _user_patch(integration_client, cid, resolved=False).status_code == 200

    notes = _notes(author)
    assert len(notes) == 1 and notes[0].endswith(f"resolved your comment on {doc.stem}.")


@pytest.mark.asyncio
async def test_an_author_resolving_its_own_comment_sends_no_note(integration_client, doc, _registrable_roots):
    _owner, author, _bystander, cid = await _setup(integration_client, doc)

    assert json.loads(await _tool_update(author, cid, resolved=True))["comment"]["resolved"] == 1

    assert _notes(author) == []


@pytest.mark.asyncio
async def test_a_user_comment_resolved_by_an_agent_sends_no_note(integration_client, doc, _registrable_roots):
    owner, author, _bystander, _cid = await _setup(integration_client, doc)
    created = integration_client.post(
        f"/api/artifacts/{_artifact_id(doc)}/comments?token={_OWNER}",
        json={"body": "User note.", "anchor_text": "A unique tail sentence", "from_off": 0, "to_off": 5},
    )
    assert created.status_code == 200, created.text
    user_cid = created.json()["comment"]["id"]

    await _tool_update(owner, user_cid, resolved=True)

    assert _notes(author) == [] and _notes(owner) == []


def _artifact_id(doc):
    from infra.database import get_db
    from registry import service

    db = get_db()
    try:
        return service.find_file_identity(str(doc), db=db)["id"]
    finally:
        db.close()
