"""Suggestion acceptance attributes commits only to persisted person principals."""
from __future__ import annotations

import hashlib

import pytest

from delegation_docs_fixtures import _OWNER, _registrable_roots, doc
from test_doc_suggestions import _own, _thread_facts

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_plain_batch_uses_only_the_accepted_person_winner_for_commit_authorship(
    integration_client, doc, _registrable_roots,
):
    from doceditor import comments
    from doceditor.comment_batch_actions import accept_all_open

    owner, artifact_id = await _own(integration_client, doc)
    quote = "A unique tail sentence"
    start = doc.read_text().index(quote)
    person, _ = comments.create_with_status(
        artifact_id, body="", author="Renamed Person", author_kind="user", author_user_id="person-immutable-id",
        anchor_text=quote, from_off=start, to_off=start + len(quote), replacement="A concise tail sentence",
    )
    agent, _ = comments.create_with_status(
        artifact_id, body="", author="Reviewer", author_kind="agent", author_user_id="person-immutable-id",
        author_thread_id="reviewer", anchor_text=quote, from_off=start, to_off=start + len(quote),
        replacement="An overlapping agent suggestion",
    )
    writes = []

    async def write(_path, _text, _project, _reason, expected_sha256, suggestion_author_user_id):
        writes.append((expected_sha256, suggestion_author_user_id))
        return {"revision": "r2"}

    result = await accept_all_open(
        artifact_id, user_id=_thread_facts(owner)[0], decided_by="Owner", actor_thread_id=owner, write=write,
    )

    assert result["accepted"] == [person["id"]]
    assert [item["id"] for item in result["skipped"]] == [agent["id"]]
    assert writes == [(hashlib.sha256(doc.read_bytes()).hexdigest(), "person-immutable-id")]


@pytest.mark.asyncio
async def test_plain_batch_keeps_accepting_agent_for_legacy_or_mixed_winners(
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
        artifact_id, body="", author="Legacy display", author_kind="user",
        anchor_text=first, from_off=first_start, to_off=first_start + len(first), replacement="# Revised design",
    )
    comments.create_with_status(
        artifact_id, body="", author="Reviewer", author_kind="agent", author_thread_id="reviewer",
        anchor_text=second, from_off=second_start, to_off=second_start + len(second), replacement="A revised tail",
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
