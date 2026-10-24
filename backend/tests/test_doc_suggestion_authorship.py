"""Accepted suggestions use existing person principals only for durable plain saves."""
from __future__ import annotations

import hashlib

import pytest

from delegation_docs_fixtures import _registrable_roots, doc
from test_doc_suggestions import _own, _thread_facts

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_plain_legacy_user_suggestion_uses_its_existing_author_principal(
    integration_client, doc, _registrable_roots,
):
    from doceditor import comments
    from doceditor.comment_batch_actions import accept_all_open

    owner, artifact_id = await _own(integration_client, doc)
    quote = "A unique tail sentence"
    start = doc.read_text().index(quote)
    person, _ = comments.create_with_status(
        artifact_id, body="", author="person-immutable-id", author_kind="user",
        anchor_text=quote, from_off=start, to_off=start + len(quote),
        replacement="A concise tail sentence",
    )
    writes = []

    async def write(_path, _text, _project, _reason, expected_sha256, suggestion_author_user_id):
        writes.append((expected_sha256, suggestion_author_user_id))
        return {"revision": "r2"}

    result = await accept_all_open(
        artifact_id, user_id=_thread_facts(owner)[0], decided_by="Owner", actor_thread_id=owner, write=write,
    )

    assert result["accepted"] == [person["id"]]
    assert writes == [(hashlib.sha256(doc.read_bytes()).hexdigest(), "person-immutable-id")]


@pytest.mark.asyncio
async def test_plain_mixed_winners_keep_the_accepting_agent_as_guidance_author(
    integration_client, doc, _registrable_roots,
):
    from doceditor import comments
    from doceditor.comment_batch_actions import accept_all_open

    owner, artifact_id = await _own(integration_client, doc)
    first = "# Design"
    second = "A unique tail sentence"
    first_start = doc.read_text().index(first)
    second_start = doc.read_text().index(second)
    comments.create_with_status(
        artifact_id, body="", author="person-immutable-id", author_kind="user",
        anchor_text=first, from_off=first_start, to_off=first_start + len(first),
        replacement="# Revised design",
    )
    comments.create_with_status(
        artifact_id, body="", author="reviewer", author_kind="agent", author_thread_id="reviewer",
        anchor_text=second, from_off=second_start, to_off=second_start + len(second),
        replacement="A revised tail",
    )
    writers = []

    async def write(_path, _text, _project, _reason, _expected_sha256, suggestion_author_user_id):
        writers.append(suggestion_author_user_id)
        return {"revision": "r2"}

    result = await accept_all_open(
        artifact_id, user_id=_thread_facts(owner)[0], decided_by="Owner", actor_thread_id=owner, write=write,
    )

    assert len(result["accepted"]) == 2
    assert writers == [None]


def test_persisted_user_selection_requires_unanimous_nonempty_user_winners():
    from doceditor.suggestion_authorship import accepted_person_author_user_id

    user = {"id": "one", "author": "person", "author_kind": "user"}
    same_user = {"id": "two", "author": "person", "author_kind": "user"}
    another_user = {"id": "three", "author": "other-person", "author_kind": "user"}
    agent = {"id": "four", "author": "reviewer", "author_kind": "agent"}
    unknown = {"id": "five", "author": "person", "author_kind": "unknown"}

    assert accepted_person_author_user_id([user, same_user], ["one", "two"]) == "person"
    assert accepted_person_author_user_id([user, another_user], ["one", "three"]) is None
    assert accepted_person_author_user_id([user, agent], ["one", "four"]) is None
    assert accepted_person_author_user_id([user, unknown], ["one", "five"]) is None
    assert accepted_person_author_user_id([user], []) is None
    assert accepted_person_author_user_id([user], ["missing"]) is None
