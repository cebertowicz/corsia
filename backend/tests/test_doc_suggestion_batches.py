"""Batch suggestion decisions use one rule set and one guarded document write."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading

import pytest
from doceditor.document_save import DocumentSaveError

from delegation_docs_fixtures import (
    _OWNER,
    _mk_thread,
    _patch,
    _registrable_roots,
    doc,
)
from test_doc_suggestions import (
    _own,
    _own_in_folder,
    _row,
    _suggest,
    _thread_facts,
)

pytestmark = pytest.mark.integration

ADAPTERS = ("http", "native", "doc-cli")
CHANGED_MESSAGE = (
    "The document changed while these suggestions were being applied. "
    "Nothing was changed; try again."
)


async def _owned_doc(adapter: str, client, document):
    if adapter == "http":
        owner, _artifact_id = await _own(client, document)
        return owner, document
    return await _own_in_folder(client, document)


async def _decide(
    adapter: str,
    client,
    owner: str,
    comment_id: str,
    decision: str,
    *,
    all_from_author: bool,
) -> tuple[int, dict]:
    payload = {"decision": decision, "all_from_author": all_from_author}
    if adapter == "http":
        response = client.post(
            f"/api/comments/{comment_id}/decision?token={_OWNER}",
            json=payload,
        )
        return response.status_code, response.json()
    if adapter == "doc-cli":
        response = _patch(
            client,
            _OWNER,
            comment_id,
            **payload,
            as_session_id=owner,
        )
        return response.status_code, response.json()

    from tools.documents import DocCommentUpdateTool

    raw = await DocCommentUpdateTool().execute(
        comment_id=comment_id,
        **payload,
        _thread_id=owner,
    )
    if raw.startswith("Error: "):
        return 409, {"detail": raw.removeprefix("Error: ")}
    return 200, json.loads(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_overlap_skips_later_suggestion_through_every_adapter(
    adapter,
    integration_client,
    doc,
    _registrable_roots,
    monkeypatch,
):
    from doceditor import document_save

    owner, doc = await _owned_doc(adapter, integration_client, doc)
    author = _mk_thread(integration_client, _OWNER, title="Batch author")
    earlier = await _suggest(
        author,
        doc,
        quote="A unique tail sentence",
        replacement="A concise sentence",
    )
    # An agent can no longer create this overlap (it is refused at creation), but one made before that rule,
    # or by a person, still reaches the batch, so the row is written at the store.
    quote = "unique tail sentence anchors"
    start = doc.read_text().index(quote)
    from doceditor import comments

    later, _ = comments.create_with_status(
        earlier["artifact_id"], body="", author="Batch author", author_kind="agent", author_thread_id=author,
        anchor_text=quote, from_off=start, to_off=start + len(quote), replacement="replacement that overlaps",
    )
    writes = []
    original_write = document_save._write_document

    def counted_write(*args, **kwargs):
        writes.append(args[1])
        return original_write(*args, **kwargs)

    monkeypatch.setattr(document_save, "_write_document", counted_write)
    status, result = await _decide(
        adapter,
        integration_client,
        owner,
        earlier["id"],
        "accept",
        all_from_author=True,
    )

    assert status == 200, result
    assert result["accepted"] == [earlier["id"]]
    assert result["skipped"] == [{
        "id": later["id"],
        "reason": "overlaps an earlier suggestion from the same author",
    }]
    assert result["revision"]
    assert len(writes) == 1
    assert "A concise sentence anchors here." in doc.read_text()
    assert _row(earlier["id"])["decision"] == "accepted"
    assert _row(later["id"])["decision"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_stale_revision_refuses_entire_batch_through_every_adapter(
    adapter,
    integration_client,
    doc,
    _registrable_roots,
    monkeypatch,
):
    from doceditor import comment_batch_actions

    owner, doc = await _owned_doc(adapter, integration_client, doc)
    author = _mk_thread(integration_client, _OWNER, title="Batch author")
    first = await _suggest(author, doc, quote="# Design", replacement="# New design")
    second = await _suggest(
        author,
        doc,
        quote="A unique tail sentence",
        replacement="A new tail sentence",
    )
    original_read = comment_batch_actions.read_doc_text
    concurrent_text = doc.read_text() + "\nConcurrent edit.\n"
    read_threads: list[int] = []

    def read_then_change(path):
        read_threads.append(threading.get_ident())
        snapshot = original_read(path)
        doc.write_text(concurrent_text)
        return snapshot

    monkeypatch.setattr(comment_batch_actions, "read_doc_text", read_then_change)
    status, result = await _decide(
        adapter,
        integration_client,
        owner,
        first["id"],
        "accept",
        all_from_author=True,
    )

    assert status == 409
    assert read_threads and threading.get_ident() not in read_threads
    assert result["detail"] == CHANGED_MESSAGE
    assert doc.read_text() == concurrent_text
    assert _row(first["id"])["decision"] is None
    assert _row(second["id"])["decision"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_reject_all_preserves_an_already_accepted_suggestion_through_every_adapter(
    adapter,
    integration_client,
    doc,
    _registrable_roots,
):
    owner, doc = await _owned_doc(adapter, integration_client, doc)
    author = _mk_thread(integration_client, _OWNER, title="Batch author")
    accepted = await _suggest(author, doc, quote="# Design", replacement="# Accepted design")
    remaining = await _suggest(
        author,
        doc,
        quote="A unique tail sentence",
        replacement="A rejected tail sentence",
    )

    status, result = await _decide(
        adapter,
        integration_client,
        owner,
        accepted["id"],
        "accept",
        all_from_author=False,
    )
    assert status == 200, result

    status, result = await _decide(
        adapter,
        integration_client,
        owner,
        remaining["id"],
        "reject",
        all_from_author=True,
    )
    assert status == 200, result
    assert _row(accepted["id"])["decision"] == "accepted"
    assert _row(remaining["id"])["decision"] == "rejected"
    assert "# Accepted design" in doc.read_text()
    assert "A unique tail sentence" in doc.read_text()


@pytest.mark.asyncio
async def test_save_document_text_writes_when_expected_hash_matches(
    integration_client,
    doc,
    _registrable_roots,
):
    from doceditor.document_save import DocumentSave, save_document_text

    owner, _ = await _own(integration_client, doc)
    user_id = _thread_facts(owner)[0]
    expected = hashlib.sha256(doc.read_bytes()).hexdigest()

    receipt = await save_document_text(
        DocumentSave(path=str(doc), text="# Matching hash\n"),
        user_id,
        expected_sha256=expected,
    )

    assert doc.read_text() == "# Matching hash\n"
    assert receipt["revision"]


@pytest.mark.asyncio
async def test_save_document_text_refuses_stale_hash_without_writing(
    integration_client,
    doc,
    _registrable_roots,
):
    from doceditor.document_save import DocumentSave, save_document_text

    owner, _ = await _own(integration_client, doc)
    user_id = _thread_facts(owner)[0]
    before = doc.read_text()

    with pytest.raises(DocumentSaveError) as refused:
        await save_document_text(
            DocumentSave(path=str(doc), text="# Must not be written\n"),
            user_id,
            expected_sha256="0" * 64,
        )

    assert refused.value.kind == "stale"
    assert refused.value.message == CHANGED_MESSAGE
    assert doc.read_text() == before


@pytest.mark.asyncio
async def test_accepting_invalid_skill_suggestion_returns_canonical_refusal_without_writing(
    integration_client,
    integration_workspace,
    _registrable_roots,
):
    # The integration workspace is session-shared: use a name no other test owns, and remove it after.
    skill = integration_workspace / "skills" / "suggestion-refusal" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    try:
        await _refuse_invalid_skill_suggestion(integration_client, skill)
    finally:
        shutil.rmtree(skill.parent)


async def _refuse_invalid_skill_suggestion(integration_client, skill) -> None:
    from core.skill_validation import validate_skill_text
    from doceditor.comment_actions import decide_suggestion
    from doceditor.document_save import agent_document_writer
    from infra.skill_save_validation import invalid_skill_line

    skill.write_text("---\nname: suggestion-refusal\ndescription: A valid skill.\n---\n# Demo\n")
    owner, _ = await _own(integration_client, skill)
    suggestion = await _suggest(
        owner, skill, quote="description: A valid skill.", replacement="",
    )
    before = skill.read_text()
    user_id = _thread_facts(owner)[0]
    expected_text = before.replace("description: A valid skill.", "")
    errors = tuple(validate_skill_text(expected_text).errors)

    with pytest.raises(DocumentSaveError) as refused:
        await decide_suggestion(
            suggestion["id"],
            "accept",
            user_id=user_id,
            decided_by="Owner",
            actor_thread_id=owner,
            write=agent_document_writer(owner, user_id),
        )

    expected = invalid_skill_line(skill, errors)
    assert refused.value.kind == "invalid_skill"
    assert refused.value.message == expected
    assert refused.value.detail == {
        "message": "Invalid SKILL.md", "errors": list(errors), "line": expected,
    }
    assert skill.read_text() == before
    assert _row(suggestion["id"])["decision"] is None


@pytest.mark.asyncio
async def test_single_accept_passes_the_snapshot_hash_to_its_writer(
    integration_client,
    doc,
    _registrable_roots,
):
    from doceditor import comments
    from doceditor.comment_actions import decide_suggestion

    owner, artifact_id = await _own(integration_client, doc)
    quote = "A unique tail sentence"
    start = doc.read_text().index(quote)
    user_id = _thread_facts(owner)[0]
    suggestion, _ = comments.create_with_status(
        artifact_id,
        body="",
        author="Person",
        author_kind="user",
        author_user_id=user_id,
        anchor_text=quote,
        from_off=start,
        to_off=start + len(quote),
        replacement="A rewritten tail sentence",
    )
    expected = hashlib.sha256(doc.read_bytes()).hexdigest()
    writes = []

    async def write(_path, _text, _project, _reason, expected_sha256=None, suggestion_author=None):
        writes.append((expected_sha256, suggestion_author))

    await decide_suggestion(
        suggestion["id"],
        "accept",
        user_id=user_id,
        decided_by="Owner",
        actor_thread_id=None,
        write=write,
    )

    assert writes == [(expected, user_id)]


@pytest.mark.asyncio
async def test_accept_all_passes_one_snapshot_hash_to_its_writer(
    integration_client,
    doc,
    _registrable_roots,
):
    from doceditor.comment_batch_actions import decide_all_from_author

    owner, _ = await _own(integration_client, doc)
    first = await _suggest(owner, doc, quote="# Design", replacement="# New design")
    await _suggest(
        owner,
        doc,
        quote="A unique tail sentence",
        replacement="A new tail sentence",
    )
    user_id = _thread_facts(owner)[0]
    expected = hashlib.sha256(doc.read_bytes()).hexdigest()
    hashes = []

    async def write(_path, _text, _project, _reason, expected_sha256=None, _suggestion_author=None):
        hashes.append(expected_sha256)
        return {"revision": "r2"}

    result = await decide_all_from_author(
        first["id"],
        "accept",
        user_id=user_id,
        decided_by="Owner",
        actor_thread_id=None,
        write=write,
    )

    assert hashes == [expected]
    assert result["revision"] == "r2"


@pytest.mark.asyncio
async def test_plain_batch_uses_the_unanimous_persisted_user_of_accepted_winners(
    integration_client, doc, _registrable_roots,
):
    from doceditor import comments
    from doceditor.comment_batch_actions import accept_all_open

    owner, artifact_id = await _own(integration_client, doc)
    quote = "A unique tail sentence"
    start = doc.read_text().index(quote)
    person, _ = comments.create_with_status(
        artifact_id, body="", author="Renamed Person", author_kind="user",
        author_user_id="person-immutable-id", anchor_text=quote, from_off=start,
        to_off=start + len(quote), replacement="A concise tail sentence",
    )
    comments.create_with_status(
        artifact_id, body="", author="Reviewer", author_kind="agent", author_thread_id="reviewer",
        anchor_text=quote, from_off=start, to_off=start + len(quote), replacement="An overlapping agent suggestion",
    )
    writers = []

    async def write(_path, _text, _project, _reason, _expected_sha256, suggestion_author):
        writers.append(suggestion_author)
        return {"revision": "r2"}

    result = await accept_all_open(
        artifact_id, user_id=_thread_facts(owner)[0], decided_by="Owner", actor_thread_id=owner, write=write,
    )

    assert result["accepted"] == [person["id"]]
    assert writers == ["person-immutable-id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["legacy", "agent", "mixed", "different_users"])
async def test_plain_batch_falls_back_for_legacy_agent_mixed_or_different_user_winners(
    integration_client, doc, _registrable_roots, case,
):
    from doceditor import comments
    from doceditor.comment_batch_actions import accept_all_open

    owner, artifact_id = await _own(integration_client, doc)
    first = "# Design"
    second = "A unique tail sentence"
    first_start = doc.read_text().index(first)
    second_start = doc.read_text().index(second)
    if case in {"legacy", "mixed"}:
        comments.create_with_status(
            artifact_id, body="", author="Legacy display", author_kind="user",
            author_user_id=None, anchor_text=first, from_off=first_start,
            to_off=first_start + len(first), replacement="# Revised design",
        )
    if case == "different_users":
        comments.create_with_status(
            artifact_id, body="", author="First user", author_kind="user", author_user_id="first-user",
            anchor_text=first, from_off=first_start, to_off=first_start + len(first), replacement="# Revised design",
        )
        comments.create_with_status(
            artifact_id, body="", author="Second user", author_kind="user", author_user_id="second-user",
            anchor_text=second, from_off=second_start, to_off=second_start + len(second), replacement="A revised tail",
        )
    if case == "agent":
        comments.create_with_status(
            artifact_id, body="", author="Reviewer", author_kind="agent", author_thread_id="reviewer",
            anchor_text=first, from_off=first_start, to_off=first_start + len(first), replacement="# Revised design",
        )
    if case == "mixed":
        comments.create_with_status(
            artifact_id, body="", author="Reviewer", author_kind="agent", author_thread_id="reviewer",
            anchor_text=second, from_off=second_start, to_off=second_start + len(second), replacement="A revised tail",
        )
    writers = []

    async def write(_path, _text, _project, _reason, _expected_sha256, suggestion_author):
        writers.append(suggestion_author)
        return {"revision": "r2"}

    result = await accept_all_open(
        artifact_id, user_id=_thread_facts(owner)[0], decided_by="Owner", actor_thread_id=owner, write=write,
    )

    assert result["accepted"]
    assert writers == [None]
