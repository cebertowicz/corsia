"""A person who muted a comment chat is brought back into it when an agent tags them (design prototype)."""

import json

import pytest

from auth_fixtures import OWNER_TOKEN
from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc

pytestmark = pytest.mark.integration


def _listed(integration_client, artifact_id: str) -> dict[str, dict]:
    listed = integration_client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN})
    assert listed.status_code == 200, listed.text
    return {thread["id"]: thread for thread in listed.json()["comments"]}


def _mute(integration_client, comment_id: str, left: bool) -> None:
    done = integration_client.patch(f"/api/comments/{comment_id}", params={"token": OWNER_TOKEN}, json={"left": left})
    assert done.status_code == 200, done.text


@pytest.mark.asyncio
async def test_muting_stops_replies_and_a_tag_of_the_person_brings_them_back(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="A question", _thread_id=writer,
    ))["comment"]
    artifact_id = root["artifact_id"]

    _mute(integration_client, root["id"], True)
    assert _listed(integration_client, artifact_id)[root["id"]]["left_by_you"] is True

    # An ordinary reply leaves the chat muted.
    await DocCommentCreateTool().execute(parent_id=root["id"], body="An ordinary reply", _thread_id=writer)
    assert _listed(integration_client, artifact_id)[root["id"]]["left_by_you"] is True

    # A reply that tags the person unmutes it.
    await DocCommentCreateTool().execute(parent_id=root["id"], body="Needs your call", tag_person=True, _thread_id=writer)
    assert _listed(integration_client, artifact_id)[root["id"]]["left_by_you"] is False
