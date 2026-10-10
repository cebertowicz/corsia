"""DCOL-FR10.2: the agent decision route tells the suggester like the user route and the tool do."""

from __future__ import annotations

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _patch, _registrable_roots, doc
from test_doc_suggestions import _own, _own_in_folder, _row, _suggest

pytestmark = pytest.mark.integration

SECOND_QUOTE = "# Design"


@pytest.fixture
def driven(monkeypatch):
    from plans import public

    calls: list[tuple[str, ...]] = []

    async def drive_threads(thread_ids):
        calls.append(tuple(thread_ids))

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    return calls


def _rejection_notes(thread_id: str) -> list[str]:
    from infra.database import get_db

    db = get_db()
    try:
        return [
            row["summary"] for row in db.execute(
                "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?",
                (thread_id,),
            ).fetchall()
        ]
    finally:
        db.close()


def _private_keys(value, path="") -> list[str]:
    if isinstance(value, dict):
        found = [f"{path}/{key}" for key in value if str(key).startswith("_")]
        for key, inner in value.items():
            found += _private_keys(inner, f"{path}/{key}")
        return found
    if isinstance(value, list):
        return [key for item in value for key in _private_keys(item, path)]
    return []


@pytest.mark.asyncio
async def test_agent_reject_with_reason_carries_it_in_a_note_and_wakes_nobody(
    integration_client, doc, _registrable_roots, driven,
):
    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, doc)

    response = _patch(
        integration_client, _OWNER, suggestion["id"], decision="reject",
        reason="Keeps the old wording on purpose.", as_session_id=owner,
    )

    assert response.status_code == 200, response.text
    assert any("Keeps the old wording on purpose." in text for text in _rejection_notes(reviewer))
    assert driven == []
    assert _private_keys(response.json()) == []


@pytest.mark.asyncio
async def test_agent_reject_all_from_author_sends_one_note_and_leaks_nothing(
    integration_client, doc, _registrable_roots, driven,
):
    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    await _suggest(reviewer, doc)
    await _suggest(reviewer, doc, replacement="A new title", quote=SECOND_QUOTE)
    first = (await _first_suggestion_id(reviewer))

    response = _patch(
        integration_client, _OWNER, first, decision="reject", all_from_author=True,
        reason="Both stay as written.", as_session_id=owner,
    )

    assert response.status_code == 200, response.text
    notes = _rejection_notes(reviewer)
    assert len(notes) == 1 and "Both stay as written." in notes[0]
    assert driven == []
    assert _private_keys(response.json()) == []


async def _first_suggestion_id(thread_id: str) -> str:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT id FROM doc_comments WHERE author_thread_id=? AND replacement IS NOT NULL ORDER BY rowid",
            (thread_id,),
        ).fetchone()["id"]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("all_from_author", [False, True])
async def test_agent_reject_without_reason_has_no_private_keys(
    integration_client, doc, _registrable_roots, driven, all_from_author,
):
    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, doc)

    response = _patch(
        integration_client, _OWNER, suggestion["id"], decision="reject",
        all_from_author=all_from_author, as_session_id=owner,
    )

    assert response.status_code == 200, response.text
    assert driven == []
    assert _private_keys(response.json()) == []


@pytest.mark.asyncio
async def test_agent_accept_response_has_no_private_keys(integration_client, doc, _registrable_roots, driven):
    owner, moved = await _own_in_folder(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, moved)

    response = _patch(integration_client, _OWNER, suggestion["id"], decision="accept", as_session_id=owner)

    assert response.status_code == 200, response.text
    assert _private_keys(response.json()) == []


@pytest.mark.asyncio
async def test_agent_reason_over_the_cap_is_refused_and_nothing_is_decided(
    integration_client, doc, _registrable_roots, driven,
):
    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, doc)

    response = _patch(
        integration_client, _OWNER, suggestion["id"], decision="reject",
        reason="x" * 2_001, as_session_id=owner,
    )

    assert response.status_code == 422, response.text
    assert "2000" in response.text
    assert _row(suggestion["id"])["decision"] is None
    assert _rejection_notes(reviewer) == []
    assert driven == []


def test_agent_patch_reason_is_optional_and_bounded_like_the_user_route():
    from api.delegation_docs import AgentCommentPatch
    from api.doc_comments import CommentDecision

    assert (
        AgentCommentPatch.model_json_schema()["properties"]["reason"]["maxLength"]
        == CommentDecision.model_json_schema()["properties"]["reason"]["maxLength"]
        == 2_000
    )


@pytest.mark.asyncio
async def test_agent_route_refuses_a_reason_without_a_decision(integration_client, doc, _registrable_roots, driven):
    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, doc)

    response = _patch(
        integration_client, _OWNER, suggestion["id"], acked=True,
        reason="Dropped without a word before.", as_session_id=owner,
    )

    assert response.status_code == 400, response.text
    assert "reason requires decision" in response.text
    assert not _row(suggestion["id"])["acked"]
    assert driven == []


@pytest.mark.asyncio
async def test_tool_refuses_a_reason_without_a_decision(integration_client, doc, _registrable_roots, driven):
    from tools.documents import DocCommentUpdateTool

    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, doc)

    result = await DocCommentUpdateTool().execute(
        comment_id=suggestion["id"], acked=True, reason="Dropped without a word before.", _thread_id=owner,
    )

    assert result == "Error: reason requires decision"
    assert not _row(suggestion["id"])["acked"]
    assert driven == []
