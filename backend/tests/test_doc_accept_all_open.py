"""Accept every open suggestion at once: one write, every author, and the review bar's verdict untouched."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from auth_fixtures import OTHER_USER, OWNER_TOKEN, OWNER_USER

from delegation_docs_fixtures import _OTHER, _OWNER, _mk_thread, _registrable_roots, doc
from test_doc_suggestions import _own, _row, _suggest, _user_decide
from test_review_bar_http import _clear, _review

pytestmark = pytest.mark.integration

HEADING, TAIL = "# Design", "A unique tail sentence"
BODY = "# Design\n\nThe conductor owns the graph.\n\nNothing else repeats here.\nA unique tail sentence anchors here.\n"


def _accept_all(client, artifact_id: str):
    return client.post(f"/api/artifacts/{artifact_id}/suggestions/accept-all?token={_OWNER}")


def _count_writes(monkeypatch) -> list[str]:
    from doceditor import document_save

    writes: list[str] = []
    original = document_save._write_document

    def counted(*args, **kwargs):
        writes.append(str(args[0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(document_save, "_write_document", counted)
    return writes


def _notes(thread_id: str) -> list[str]:
    from infra.database import get_db

    db = get_db()
    try:
        return [row["summary"] for row in db.execute(
            "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=? "
            "ORDER BY seq", (thread_id,))]
    finally:
        db.close()


def _raise_ask(artifact_id: str) -> None:
    """Raise a review ask from the document's owner, set to a model that runs no turn, so answering it wakes nothing."""
    from infra.database import get_db
    from plans import service
    from plans.boundary import PlanActor, mutate

    db = get_db()
    try:
        from registry import models as registry_models

        thread_id = registry_models.get_artifact(artifact_id, db=db)["parent_thread_id"]
        db.execute("UPDATE threads SET model=? WHERE id=?", ("openai/test-checkpoint", thread_id))
        db.commit()
        plan = service.ensure_plan("thread", thread_id, db=db)
        node = service.add_node(
            plan.id, kind="task", title="Review the proposal", acceptance_criteria="Reviewed", db=db)
        db.commit()
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Read the proposal",
                "owner_kind": "user", "owner_id": None, "ask_artifact_id": artifact_id,
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=PlanActor(thread_id, OWNER_USER, None), provenance="test.accept_all",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_accepts_every_authors_open_suggestions_in_one_write_and_skips_a_changed_one(
    integration_client, doc, _registrable_roots, monkeypatch,
):
    doc.write_text(BODY)
    owner, artifact_id = await _own(integration_client, doc)
    first = _mk_thread(integration_client, _OWNER, title="First helper")
    second = _mk_thread(integration_client, _OWNER, title="Second helper")
    a = await _suggest(first, doc, replacement="# New design", quote=HEADING)
    b = await _suggest(second, doc, replacement="A concise tail sentence", quote=TAIL)
    c = await _suggest(first, doc, replacement="The conductor holds", quote="The conductor owns")
    # The third suggestion's text is edited away, so it can no longer apply.
    doc.write_text(doc.read_text().replace("The conductor owns", "Somebody runs"))
    writes = _count_writes(monkeypatch)

    response = _accept_all(integration_client, artifact_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["accepted"]) == {a["id"], b["id"]}
    assert [item["id"] for item in body["skipped"]] == [c["id"]]
    assert len(writes) == 1
    text = doc.read_text()
    assert "# New design" in text and "A concise tail sentence anchors here." in text
    assert _row(a["id"])["decision"] == _row(b["id"])["decision"] == "accepted"
    assert _row(c["id"])["decision"] is None
    for thread in (first, second):
        accepted_notes = [note for note in _notes(thread) if " accepted your suggestion on " in note]
        assert len(accepted_notes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("other_outcome", ["skipped", "rejected"])
async def test_accept_all_credits_an_api_created_person_suggestion_despite_another_persons_nonaccepted_row(
    integration_client, doc, _registrable_roots, monkeypatch, other_outcome,
):
    """A shared UI artifact lets the creator and accepting person use their real HTTP routes."""
    from api import doc_comments

    doc.write_text("# Design\n\nA unique tail sentence\n")
    registered = integration_client.post(
        f"/api/files/register?token={_OWNER}", json={"path": str(doc)},
    )
    assert registered.status_code == 200, registered.text
    artifact_id = registered.json()["artifact"]["id"]
    assert registered.json()["artifact"]["parent_thread_id"] == "ui"

    def create(token, quote, replacement):
        start = doc.read_text().index(quote)
        response = integration_client.post(
            f"/api/artifacts/{artifact_id}/comments?token={token}",
            json={"delivery_policy": "immediate", 
                "body": "", "anchor_text": quote, "from_off": start, "to_off": start + len(quote),
                "replacement": replacement,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["comment"]

    accepted = create(_OTHER, HEADING, "# Revised design")
    other = create(_OWNER, TAIL, "A revised tail sentence")
    assert accepted["author"] == OTHER_USER
    assert OTHER_USER != OWNER_USER

    if other_outcome == "skipped":
        doc.write_text(doc.read_text().replace(TAIL, "A changed tail sentence"))
    else:
        rejected = _user_decide(integration_client, other["id"], "reject")
        assert rejected.status_code == 200, rejected.text

    captured = []
    original_person_writer = doc_comments._person_writer

    def capture_person_writer(user, saved):
        writer = original_person_writer(user, saved)

        async def write(path, text, project_id, reason, expected_sha256, suggestion_author):
            captured.append((user["id"], suggestion_author))
            return await writer(path, text, project_id, reason, expected_sha256, suggestion_author)

        return write

    monkeypatch.setattr(doc_comments, "_person_writer", capture_person_writer)
    response = _accept_all(integration_client, artifact_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] == [accepted["id"]]
    assert len(body["skipped"]) == (1 if other_outcome == "skipped" else 0)
    assert captured == [(OWNER_USER, OTHER_USER)]
    assert _row(accepted["id"])["decision"] == "accepted"
    assert _row(other["id"])["decision"] == (None if other_outcome == "skipped" else "rejected")


@pytest.mark.asyncio
async def test_with_nothing_open_it_changes_nothing_and_is_not_an_error(
    integration_client, doc, _registrable_roots, monkeypatch,
):
    _owner, artifact_id = await _own(integration_client, doc)
    writes = _count_writes(monkeypatch)

    response = _accept_all(integration_client, artifact_id)

    assert response.status_code == 200
    assert (response.json()["accepted"], response.json()["skipped"]) == ([], [])
    assert writes == []


@pytest.mark.asyncio
async def test_a_stranger_cannot_accept_on_a_document_they_do_not_own(integration_client, doc, _registrable_roots):
    _owner, artifact_id = await _own(integration_client, doc)

    response = integration_client.post(f"/api/artifacts/{artifact_id}/suggestions/accept-all?token={_OTHER}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bulk_is_true_for_both_bulk_paths_and_false_for_a_single_decision(
    integration_client, doc, _registrable_roots,
):
    doc.write_text(BODY)
    _owner, artifact_id = await _own(integration_client, doc)
    helper = _mk_thread(integration_client, _OWNER, title="Helper")
    single = await _suggest(helper, doc, replacement="# New design", quote=HEADING)
    by_author = await _suggest(helper, doc, replacement="A concise tail sentence", quote=TAIL)
    pending = await _suggest(helper, doc, replacement="The conductor holds", quote="The conductor owns")
    assert _row(single["id"])["decided_in_bulk"] is None

    assert _user_decide(integration_client, single["id"], "accept").status_code == 200
    assert _row(single["id"])["decided_in_bulk"] == 0

    from_author = integration_client.post(
        f"/api/comments/{by_author['id']}/decision?token={_OWNER}",
        json={"decision": "reject", "all_from_author": True})
    assert from_author.status_code == 200
    assert _row(by_author["id"])["decided_in_bulk"] == 1
    assert _row(pending["id"])["decided_in_bulk"] == 1

    reopened = await _suggest(helper, doc, replacement="A final tail sentence", quote="A unique tail")
    assert _accept_all(integration_client, artifact_id).status_code == 200
    assert _row(reopened["id"])["decided_in_bulk"] == 1


@pytest.mark.asyncio
async def test_reopening_a_rejected_suggestion_clears_its_bulk_flag(integration_client, doc, _registrable_roots):
    await _own(integration_client, doc)
    helper = _mk_thread(integration_client, _OWNER, title="Helper")
    suggestion = await _suggest(helper, doc, replacement="# New design", quote=HEADING)
    integration_client.post(
        f"/api/comments/{suggestion['id']}/decision?token={_OWNER}",
        json={"decision": "reject", "all_from_author": True})
    assert _row(suggestion["id"])["decided_in_bulk"] == 1

    reopened = integration_client.patch(
        f"/api/comments/{suggestion['id']}?token={_OWNER}", json={"delivery_policy": "document-held", "resolved": False})

    assert reopened.status_code == 200
    assert _row(suggestion["id"])["decided_in_bulk"] is None


@pytest.mark.asyncio
async def test_the_bars_own_accept_does_not_count_as_the_persons_round_feedback(
    integration_client, doc, _registrable_roots,
):
    _owner, artifact_id = await _own(integration_client, doc)
    helper = _mk_thread(integration_client, _OWNER, title="Helper")
    await _suggest(helper, doc, replacement="# New design", quote=HEADING)
    await _suggest(helper, doc, replacement="A concise tail sentence", quote=TAIL)
    await asyncio.to_thread(_raise_ask, artifact_id)
    before = _review(integration_client, artifact_id)

    assert _accept_all(integration_client, artifact_id).status_code == 200

    after = _review(integration_client, artifact_id)
    assert (before["round_feedback_count"], after["round_feedback_count"]) == (0, 0)
    assert after["round_feedback_counts"] == {"comments": 0, "suggestions": 0, "edits": 0}
    answered = _clear(integration_client, artifact_id, verdict="approved")
    assert answered.status_code == 200
    assert answered.json()["review"]["verdict"] == "approved"
    assert answered.json()["review"]["feedback_counts"] == {"comments": 0, "suggestions": 0, "edits": 0}


@pytest.mark.asyncio
async def test_the_persons_own_comment_still_makes_it_approved_with_changes(
    integration_client, doc, _registrable_roots,
):
    _owner, artifact_id = await _own(integration_client, doc)
    helper = _mk_thread(integration_client, _OWNER, title="Helper")
    await _suggest(helper, doc, replacement="# New design", quote=HEADING)
    await asyncio.to_thread(_raise_ask, artifact_id)
    note = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_OWNER}",
        json={"delivery_policy": "immediate", "body": "Check this", "anchor_text": TAIL, "from_off": 0, "to_off": len(TAIL)})
    assert note.status_code == 200, note.text

    assert _accept_all(integration_client, artifact_id).status_code == 200

    after = _review(integration_client, artifact_id)
    assert after["round_feedback_counts"] == {"comments": 1, "suggestions": 0, "edits": 0}
    answered = _clear(integration_client, artifact_id, verdict="approved")
    assert answered.json()["review"]["verdict"] == "approved_with_changes"


def test_the_accept_route_is_not_open_without_a_token(integration_client):
    response = integration_client.post("/api/artifacts/nope/suggestions/accept-all")

    assert response.status_code in (401, 422)
    assert OWNER_TOKEN not in response.text
