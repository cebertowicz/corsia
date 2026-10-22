"""An agent never suggests over an open suggestion; a person may, and a dead one blocks nothing."""

from __future__ import annotations

import pytest
from auth_fixtures import OWNER_TOKEN

from delegation_docs_fixtures import _OWNER, _mk_thread, _post, _registrable_roots, doc
from test_doc_suggestions import _own, _row, _suggest, _user_decide

pytestmark = pytest.mark.integration

TAIL = "A unique tail sentence"
OVERLAPPING = "tail sentence anchors"  # shares "tail sentence" with TAIL
BODY = "# Design\n\nThe conductor owns the graph.\n\nNothing else repeats here.\nA unique tail sentence anchors here.\n"


async def _tool_create(thread_id, doc, quote, replacement="rewritten"):
    from tools.documents import DocCommentCreateTool

    return await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=quote, replacement=replacement, _thread_id=thread_id)


@pytest.mark.asyncio
async def test_the_tool_refuses_a_second_agent_suggestion_over_an_open_one(
    integration_client, doc, _registrable_roots,
):
    doc.write_text(BODY)
    await _own(integration_client, doc)
    first = _mk_thread(integration_client, _OWNER, title="First")
    second = _mk_thread(integration_client, _OWNER, title="Second")
    open_one = await _suggest(first, doc, replacement="A shorter tail", quote=TAIL)

    refused = await _tool_create(second, doc, OVERLAPPING)

    assert refused.startswith("Error: This overlaps an open suggestion")
    assert open_one["id"] in refused


@pytest.mark.asyncio
async def test_the_agent_route_refuses_it_with_a_409(integration_client, doc, _registrable_roots):
    doc.write_text(BODY)
    await _own(integration_client, doc)
    first = _mk_thread(integration_client, _OWNER, title="First")
    second = _mk_thread(integration_client, _OWNER, title="Second")
    open_one = await _suggest(first, doc, replacement="A shorter tail", quote=TAIL)

    refused = _post(integration_client, _OWNER, doc_path=str(doc), quote=OVERLAPPING,
                    replacement="rewritten", body="", as_session_id=second)

    assert refused.status_code == 409
    assert open_one["id"] in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_suggestion_that_does_not_overlap_is_created(integration_client, doc, _registrable_roots):
    doc.write_text(BODY)
    await _own(integration_client, doc)
    first = _mk_thread(integration_client, _OWNER, title="First")
    second = _mk_thread(integration_client, _OWNER, title="Second")
    await _suggest(first, doc, replacement="A shorter tail", quote=TAIL)

    created = await _suggest(second, doc, replacement="The conductor holds", quote="The conductor owns")
    adjacent = await _suggest(second, doc, replacement="# New design", quote="# Design")

    assert created["replacement"] == "The conductor holds"
    assert adjacent["replacement"] == "# New design"


@pytest.mark.asyncio
async def test_a_persons_overlapping_suggestion_is_created(integration_client, doc, _registrable_roots):
    doc.write_text(BODY)
    _owner, artifact_id = await _own(integration_client, doc)
    first = _mk_thread(integration_client, _OWNER, title="First")
    await _suggest(first, doc, replacement="A shorter tail", quote=TAIL)

    person = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN},
        json={"body": "", "anchor_text": OVERLAPPING, "replacement": "mine",
              "from_off": BODY.index(OVERLAPPING), "to_off": BODY.index(OVERLAPPING) + len(OVERLAPPING)})

    assert person.status_code == 200, person.text


@pytest.mark.asyncio
async def test_a_decided_or_orphaned_suggestion_does_not_block_a_new_one(
    integration_client, doc, _registrable_roots,
):
    doc.write_text(BODY)
    await _own(integration_client, doc)
    first = _mk_thread(integration_client, _OWNER, title="First")
    second = _mk_thread(integration_client, _OWNER, title="Second")
    decided = await _suggest(first, doc, replacement="A shorter tail", quote=TAIL)
    assert _user_decide(integration_client, decided["id"], "reject").status_code == 200
    again = await _suggest(second, doc, replacement="Another tail", quote=TAIL)
    assert _row(again["id"])["decision"] is None

    # Editing the quoted text away orphans the open suggestion, so it no longer blocks.
    doc.write_text(BODY.replace(TAIL, "Something wholly different"))
    fresh = await _suggest(first, doc, replacement="Brand new", quote="Something wholly different")

    assert fresh["replacement"] == "Brand new"
