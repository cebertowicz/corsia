"""The names a reply goes to: the document's owner first, then the others in the order they joined the comment chat."""

import json

import pytest

from auth_fixtures import OWNER_TOKEN
from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc

pytestmark = pytest.mark.integration


def _goes_to(integration_client, artifact_id: str, root_id: str) -> list[str]:
    listed = integration_client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN})
    assert listed.status_code == 200, listed.text
    thread = next(t for t in listed.json()["comments"] if t["id"] == root_id)
    return [entry["id"] for entry in thread["reply_goes_to"]]


@pytest.mark.asyncio
async def test_reply_goes_to_names_the_owner_first_then_agents_in_the_order_they_joined(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    first = _mk_thread(integration_client, _OWNER, title="First writer")
    second = _mk_thread(integration_client, _OWNER, title="Second writer")
    third = _mk_thread(integration_client, _OWNER, title="Third writer")
    create = DocCommentCreateTool()
    root = json.loads(await create.execute(doc_path=str(doc), quote="A unique tail sentence", body="A question", _thread_id=first))["comment"]
    await create.execute(parent_id=root["id"], body="Third speaks before second", _thread_id=third)
    await create.execute(parent_id=root["id"], body="Second joins last", _thread_id=second)

    got = _goes_to(integration_client, root["artifact_id"], root["id"])
    joined = [i for i in got if i in (first, second, third)]
    assert joined == [first, third, second]
    # The document's owner, when it is among the names, leads them all.
    owner = next((i for i in got if i not in (first, second, third)), None)
    if owner is not None:
        assert got[0] == owner
