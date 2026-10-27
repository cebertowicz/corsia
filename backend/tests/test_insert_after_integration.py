"""End-to-end agent creation and decision on file-backed suggestions."""
import json

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc
from tools.artifacts import ArtifactRegisterTool
from tools.documents import DocCommentCreateTool, DocCommentUpdateTool


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("order, decisions, expected", [
    ([0, 1], ["accept", "accept"], "Heading one two\n"),
    ([1, 0], ["accept", "accept"], "Heading one two\n"),
    ([0, 1], ["reject", "accept"], "Heading two\n"),
    ([0, 1], ["accept", "reject"], "Heading one\n"),
])
async def test_two_insertions_are_independently_accepted(integration_client, doc, _registrable_roots, order, decisions, expected):
    owner = _mk_thread(integration_client, _OWNER, title="Owner")
    moved = doc.parent / ".threads" / owner / doc.name
    moved.parent.mkdir(parents=True)
    doc.rename(moved)
    moved.write_text("Heading\n", encoding="utf-8")
    await ArtifactRegisterTool().execute(path=str(moved), _thread_id=owner)
    created = []
    for replacement in (" one", " two"):
        result = await DocCommentCreateTool().execute(doc_path=str(moved), quote="Heading", replacement=replacement,
                                                       insert_after=True, _thread_id=owner)
        created.append(json.loads(result)["comment"])
    assert all(item["insert_after"] for item in created)
    for index, decision in zip(order, decisions):
        result = await DocCommentUpdateTool().execute(comment_id=created[index]["id"], decision=decision, _thread_id=owner)
        assert not result.startswith("Error"), result
    assert moved.read_text(encoding="utf-8") == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_does_not_read_accepted_siblings(integration_client, doc, _registrable_roots, monkeypatch):
    from doceditor import comments

    owner = _mk_thread(integration_client, _OWNER, title="Owner")
    moved = doc.parent / ".threads" / owner / doc.name
    moved.parent.mkdir(parents=True)
    doc.rename(moved)
    moved.write_text("Heading\n", encoding="utf-8")
    await ArtifactRegisterTool().execute(path=str(moved), _thread_id=owner)
    result = await DocCommentCreateTool().execute(doc_path=str(moved), quote="Heading", replacement=" fact",
                                                   insert_after=True, _thread_id=owner)
    comment = json.loads(result)["comment"]
    def unexpected_read(*args, **kwargs):
        raise AssertionError("reject read accepted siblings")
    monkeypatch.setattr(comments, "accepted_insertions", unexpected_read)
    result = await DocCommentUpdateTool().execute(comment_id=comment["id"], decision="reject", _thread_id=owner)
    assert not result.startswith("Error"), result
    assert moved.read_text(encoding="utf-8") == "Heading\n"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accept_all_keeps_both_insertions(integration_client, doc, _registrable_roots):
    owner = _mk_thread(integration_client, _OWNER, title="Owner")
    moved = doc.parent / ".threads" / owner / doc.name
    moved.parent.mkdir(parents=True)
    doc.rename(moved)
    moved.write_text("Heading\n", encoding="utf-8")
    artifact = json.loads(await ArtifactRegisterTool().execute(path=str(moved), _thread_id=owner))["artifact"]["id"]
    for replacement in (" one", " two"):
        result = await DocCommentCreateTool().execute(artifact_id=artifact, quote="Heading", replacement=replacement,
                                                       insert_after=True, _thread_id=owner)
        assert not result.startswith("Error"), result
    response = integration_client.post(f"/api/artifacts/{artifact}/suggestions/accept-all", params={"token": _OWNER})
    assert response.status_code == 200, response.text
    assert len(response.json()["accepted"]) == 2
    assert moved.read_text(encoding="utf-8") == "Heading one two\n"
