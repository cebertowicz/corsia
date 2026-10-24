"""Suggested edits (DCOL-FR3.1-3.4): one rule set through the user route, the
native agent tools and the agent route the ``corsia doc`` CLI calls."""

from __future__ import annotations

import json
import sqlite3

import pytest

from delegation_docs_fixtures import (
    _OTHER,
    _OWNER,
    _mk_thread,
    _patch,
    _post,
    _registrable_roots,
    doc,
)

pytestmark = pytest.mark.integration

QUOTE = "A unique tail sentence"


async def _own(client, doc, title="Doc Owner"):
    """Register ``doc`` to a new thread, which then owns it."""
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(client, _OWNER, title=title)
    registered = json.loads(await ArtifactRegisterTool().execute(path=str(doc), _thread_id=owner))
    return owner, registered["artifact"]["id"]


async def _own_in_folder(client, doc, title="Doc Owner"):
    """Move ``doc`` into a new thread's own folder, where that agent may write, and register it there."""
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(client, _OWNER, title=title)
    moved = doc.parent / ".threads" / owner / doc.name
    moved.parent.mkdir(parents=True)
    doc.rename(moved)
    await ArtifactRegisterTool().execute(path=str(moved), _thread_id=owner)
    return owner, moved


async def _suggest(thread_id, doc, replacement="A rewritten tail sentence", quote=QUOTE):
    from tools.documents import DocCommentCreateTool

    result = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=quote, replacement=replacement, _thread_id=thread_id,
    )
    return json.loads(result)["comment"]


async def _tool_decide(thread_id, comment_id, decision):
    from tools.documents import DocCommentUpdateTool

    return await DocCommentUpdateTool().execute(
        comment_id=comment_id, decision=decision, _thread_id=thread_id,
    )


def _user_decide(client, comment_id, decision, token=_OWNER):
    return client.post(f"/api/comments/{comment_id}/decision?token={token}", json={"decision": decision})


def _thread_facts(thread_id):
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute("SELECT user_id, project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    finally:
        db.close()


def _row(comment_id):
    from doceditor import comments as store

    return store.get_comment(comment_id)


@pytest.mark.asyncio
async def test_create_validation_is_one_rule_set_across_adapters(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    owner, artifact_id = await _own(integration_client, doc)
    top = _post(integration_client, _OWNER, doc_path=str(doc), quote=QUOTE, body="top", as_session_id=owner)
    parent_id = top.json()["comment"]["id"]
    shape = "A suggestion replaces quoted text. Pass quote and replacement, without parent_id or review."
    cases = [
        ({"review": True, "replacement": "x"}, shape),
        ({"parent_id": parent_id, "replacement": "x"}, shape),
        ({"quote": "", "replacement": "x"}, "A suggestion needs quote: the exact current text to replace."),
        ({"quote": QUOTE, "replacement": QUOTE}, "replacement is identical to quote, so there is nothing to suggest."),
    ]
    for fields, message in cases:
        target = {} if "parent_id" in fields else {"doc_path": str(doc)}
        tool = await DocCommentCreateTool().execute(**target, **fields, _thread_id=owner)
        assert tool == f"Error: {message}", (fields, tool)

        route = _post(integration_client, _OWNER, **target, **fields, as_session_id=owner)
        assert (route.status_code, route.json()["detail"]) == (400, message), fields

        user_fields = {k: v for k, v in fields.items() if k != "quote"}
        if fields.get("quote"):
            user_fields.update(anchor_text=fields["quote"], from_off=0, to_off=len(fields["quote"]))
        user = integration_client.post(
            f"/api/artifacts/{artifact_id}/comments?token={_OWNER}", json={"body": "", **user_fields})
        assert (user.status_code, user.json()["detail"]) == (400, message), fields

    deletion = await _suggest(owner, doc, replacement="")
    assert deletion["replacement"] == ""


@pytest.mark.asyncio
async def test_accept_all_from_author_applies_nonoverlapping_suggestions_once(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentUpdateTool

    owner, doc = await _own_in_folder(integration_client, doc)
    author = _mk_thread(integration_client, _OWNER, title="Reviewer")
    first = await _suggest(author, doc, replacement="A revised tail sentence")
    second = await _suggest(author, doc, replacement="Document heading", quote="# Design")

    result = json.loads(await DocCommentUpdateTool().execute(
        comment_id=first["id"], decision="accept", all_from_author=True, _thread_id=owner,
    ))
    assert set(result["accepted"]) == {first["id"], second["id"]}
    assert result["skipped"] == []
    assert "A revised tail sentence" in doc.read_text()
    assert "Document heading" in doc.read_text()
    assert _row(first["id"])["decision"] == "accepted"
    assert _row(second["id"])["decision"] == "accepted"


@pytest.mark.asyncio
async def test_owner_agent_accepts_through_the_tool_and_the_cli_route(integration_client, doc, _registrable_roots):
    owner, doc = await _own_in_folder(integration_client, doc)
    suggester = _mk_thread(integration_client, _OWNER, title="Reviewer")
    first = await _suggest(suggester, doc, replacement="A rewritten tail sentence")

    accepted = json.loads(await _tool_decide(owner, first["id"], "accept"))["comment"]
    assert "A rewritten tail sentence anchors here." in doc.read_text()
    assert QUOTE not in doc.read_text()
    assert (accepted["decision"], accepted["resolved"]) == ("accepted", 1)
    assert (accepted["author_thread_id"], accepted["decided_by_thread_id"]) == (suggester, owner)
    assert accepted["decided_by"] == "Doc Owner"

    second = await _suggest(suggester, doc, quote="communication graph.\nA", replacement="graph.\nA")
    via_cli = _patch(integration_client, _OWNER, second["id"], decision="accept", as_session_id=owner)
    assert via_cli.status_code == 200, via_cli.text
    assert via_cli.json()["comment"]["decision"] == "accepted"
    assert "owns the graph.\nA rewritten" in doc.read_text()


@pytest.mark.asyncio
async def test_non_owner_agent_is_refused_with_the_correction(integration_client, doc, _registrable_roots):
    owner, _ = await _own(integration_client, doc, title="Context Owner")
    other = _mk_thread(integration_client, _OWNER, title="Not Owner")
    suggestion = await _suggest(other, doc)
    before = doc.read_text()
    refusal = ("Only the document's owner (Context Owner), the conductor, or the user can accept or reject "
               "suggestions. To propose a change, suggest it with `doc_comment_create` "
               "(`doc_path`, `quote`, `replacement`); to raise a point, reply to the suggestion.")

    assert await _tool_decide(other, suggestion["id"], "accept") == f"Error: {refusal}"
    route = _patch(integration_client, _OWNER, suggestion["id"], decision="reject", as_session_id=other)
    assert (route.status_code, route.json()["detail"]) == (403, refusal)
    assert route.json()["kind"] == "not_owner"
    assert doc.read_text() == before
    assert _row(suggestion["id"])["decision"] is None


@pytest.mark.asyncio
async def test_stale_suggestion_is_refused_and_left_undecided(integration_client, doc, _registrable_roots):
    from doceditor.comment_actions import STALE_MESSAGE

    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)
    doc.write_text(doc.read_text().replace(QUOTE, "A different tail"))

    assert await _tool_decide(owner, suggestion["id"], "accept") == f"Error: {STALE_MESSAGE}"
    user = _user_decide(integration_client, suggestion["id"], "accept")
    assert (user.status_code, user.json()["detail"]) == (409, STALE_MESSAGE)
    assert _row(suggestion["id"])["decision"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stale", "decided", "unrecorded"])
async def test_a_refused_accept_keeps_its_status_and_names_its_kind(
        kind, integration_client, doc, _registrable_roots, monkeypatch):
    """All three are 409; only `kind` tells the client which, so it can show, reload or re-read."""
    from doceditor import comment_actions

    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)
    if kind == "stale":
        doc.write_text(doc.read_text().replace(QUOTE, "A different tail"))
    elif kind == "decided":
        assert _user_decide(integration_client, suggestion["id"], "reject").status_code == 200
    else:
        def fail(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(comment_actions, "record_decision", fail)
    refused = _user_decide(integration_client, suggestion["id"], "accept")
    assert (refused.status_code, refused.json()["kind"]) == (409, kind)
    assert isinstance(refused.json()["detail"], str) and refused.json()["detail"]


@pytest.mark.asyncio
async def test_user_route_decides_and_keeps_its_access_gate(integration_client, doc, _registrable_roots):
    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)
    comment = _post(integration_client, _OWNER, doc_path=str(doc), quote="# Design",
                    body="plain", as_session_id=owner)

    assert _user_decide(integration_client, suggestion["id"], "accept", token=_OTHER).status_code == 404
    assert _user_decide(integration_client, suggestion["id"], "maybe").status_code == 400
    assert _user_decide(integration_client, comment.json()["comment"]["id"], "accept").status_code == 400
    assert _user_decide(integration_client, "no-such-comment", "accept").status_code == 404
    assert QUOTE in doc.read_text()

    accepted = _user_decide(integration_client, suggestion["id"], "accept")
    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["text"] == doc.read_text()
    assert "A rewritten tail sentence" in payload["text"] and payload["revision"]
    assert payload["comment"]["decided_by_thread_id"] is None


@pytest.mark.asyncio
async def test_rejected_suggestion_reopens_and_accepted_one_cannot(integration_client, doc, _registrable_roots):
    owner, doc = await _own_in_folder(integration_client, doc)
    rejected = await _suggest(owner, doc, replacement="Rejected tail sentence")
    before = doc.read_text()
    assert _user_decide(integration_client, rejected["id"], "reject").json()["comment"]["decision"] == "rejected"
    assert doc.read_text() == before
    again = _user_decide(integration_client, rejected["id"], "accept")
    assert (again.status_code, again.json()["detail"].startswith("This suggestion was already rejected")) == (409, True)

    reopened = integration_client.patch(f"/api/comments/{rejected['id']}?token={_OWNER}", json={"resolved": False})
    assert reopened.status_code == 200, reopened.text
    row = reopened.json()["comment"]
    assert (row["resolved"], row["decision"], row["decided_by"], row["decided_at"]) == (0, None, None, None)

    json.loads(await _tool_decide(owner, rejected["id"], "accept"))
    cannot = integration_client.patch(f"/api/comments/{rejected['id']}?token={_OWNER}", json={"resolved": False})
    assert cannot.status_code == 409
    from tools.documents import DocCommentUpdateTool
    tool = await DocCommentUpdateTool().execute(comment_id=rejected["id"], resolved=False, _thread_id=owner)
    assert tool.startswith("Error: This suggestion was accepted")
    assert _row(rejected["id"])["decision"] == "accepted"


@pytest.mark.asyncio
async def test_undecided_suggestion_cannot_be_resolved_through_user_or_tool_patch(
    integration_client, doc, _registrable_roots,
):
    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)

    route = integration_client.patch(
        f"/api/comments/{suggestion['id']}?token={_OWNER}", json={"resolved": True},
    )
    assert (route.status_code, route.json()["detail"]) == (
        409, "Accept or reject this suggestion instead of resolving it.",
    )
    from tools.documents import DocCommentUpdateTool

    tool = await DocCommentUpdateTool().execute(comment_id=suggestion["id"], resolved=True, _thread_id=owner)
    assert tool == "Error: Accept or reject this suggestion instead of resolving it."
    row = _row(suggestion["id"])
    assert (row["resolved"], row["decision"]) == (0, None)


@pytest.mark.asyncio
async def test_accept_writes_in_the_owner_threads_project_scope(integration_client, doc, _registrable_roots):
    from doceditor.comment_actions import decide_suggestion

    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)
    user_id, project_id = _thread_facts(owner)
    writes = []

    async def write(path, text, project, reason, expected_sha256=None, _suggestion_author=None):
        writes.append((path, project, reason))

    await decide_suggestion(suggestion["id"], "accept", user_id=user_id, decided_by="u",
                            actor_thread_id=None, write=write)
    reason = f"Accepted suggestion {suggestion['id']} from {suggestion['author']}"
    assert writes == [(str(doc.resolve()), project_id, reason)]


@pytest.mark.asyncio
async def test_agent_accept_keeps_the_scope_structure_rules(integration_client, doc, _registrable_roots):
    """An agent accepting a suggestion writes as itself: a scope-root file its own write_file
    could not change is refused, left undecided, and still open to the user."""
    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)
    before = doc.read_text()

    refused = await _tool_decide(owner, suggestion["id"], "accept")
    assert refused.startswith("Error: the document could not be saved: The scope root is scope structure")
    assert doc.read_text() == before
    assert _row(suggestion["id"])["decision"] is None
    user = _user_decide(integration_client, suggestion["id"], "accept")
    assert user.status_code == 200, user.text
    assert "A rewritten tail sentence" in doc.read_text()


@pytest.mark.asyncio
async def test_accept_reports_an_applied_but_unrecorded_edit(integration_client, doc, _registrable_roots, monkeypatch):
    from doceditor import comment_actions

    owner, _ = await _own(integration_client, doc)
    suggestion = await _suggest(owner, doc)
    user_id = _thread_facts(owner)[0]
    writes = []

    async def write(path, text, project, reason, expected_sha256=None, _suggestion_author=None):
        writes.append(text)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(comment_actions, "record_decision", fail)
    with pytest.raises(comment_actions.CommentActionError) as refused:
        await comment_actions.decide_suggestion(
            suggestion["id"], "accept", user_id=user_id, decided_by="u",
            actor_thread_id=None, write=write)
    assert refused.value.kind == "unrecorded"
    assert refused.value.message.startswith("The edit was applied to the document")
    assert len(writes) == 1


def test_anchoring_programming_errors_propagate(integration_client, doc, monkeypatch):
    from doceditor import comment_anchoring, comments as store
    from doceditor.quote_anchor import resolve_quote
    from infra.database import get_db

    # A contextless row at its exact span backfills context, which persists.
    from_off, to_off = resolve_quote(doc.read_text(), "A unique")
    monkeypatch.setattr(store, "list_for_artifact", lambda *_a, **_k: [{
        "id": "c", "parent_id": None, "review": 0, "anchor_text": "A unique",
        "from_off": from_off, "to_off": to_off, "anchor_prefix": None, "anchor_suffix": None,
    }])

    def broken(*_args, **_kwargs):
        raise TypeError("bug")

    monkeypatch.setattr(store, "update_positions", broken)
    db = get_db()
    try:
        with pytest.raises(TypeError):
            comment_anchoring.anchored_threads("artifact", str(doc), db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_accepting_into_a_guidance_file_records_history(integration_client, integration_workspace, _registrable_roots):
    from test_guidance_history import SKILL_BODY, _git

    _git(["init", "--quiet"], cwd=integration_workspace)
    owner = _mk_thread(integration_client, _OWNER, title="Skill Owner")
    skill = integration_workspace / ".threads" / owner / "skills" / "demo" / "SKILL.md"
    created = integration_client.put(f"/api/files/content?token={_OWNER}", json={
        "path": str(skill), "text": SKILL_BODY, "thread_id": owner, "create": True})
    assert created.status_code == 200, created.text
    quote = SKILL_BODY.strip().splitlines()[-1]
    suggestion = await _suggest(owner, skill, quote=quote, replacement=quote + " Now clearer.")

    relative = f".threads/{owner}/skills/demo/SKILL.md"
    before = _git(["rev-list", "--count", "HEAD", "--", relative], cwd=integration_workspace).strip()
    accepted = json.loads(await _tool_decide(owner, suggestion["id"], "accept"))["comment"]
    assert accepted["decision"] == "accepted"
    after = _git(["rev-list", "--count", "HEAD", "--", relative], cwd=integration_workspace).strip()
    assert int(after) == int(before) + 1
    assert skill.read_text(encoding="utf-8").rstrip().endswith("Now clearer.")
    author, message = _git(
        ["log", "-1", "--format=%ae%x00%B", "--", relative], cwd=integration_workspace).split("\x00", 1)
    assert author == f"{owner}@agents.corsia.local"
    assert f"Accepted suggestion {suggestion['id']} from {suggestion['author']}" in message


@pytest.mark.asyncio
async def test_browser_accept_of_another_persons_suggestion_uses_that_person_as_the_durable_author(
    integration_client, integration_workspace, _registrable_roots,
):
    from doceditor import comments
    from infra.database import get_db
    from test_guidance_history import SKILL_BODY, _git

    _git(["init", "--quiet"], cwd=integration_workspace)
    owner = _mk_thread(integration_client, _OWNER, title="Skill Owner")
    skill = integration_workspace / ".threads" / owner / "skills" / "demo" / "SKILL.md"
    created = integration_client.put(f"/api/files/content?token={_OWNER}", json={
        "path": str(skill), "text": SKILL_BODY, "thread_id": owner, "create": True})
    assert created.status_code == 200, created.text
    quote = SKILL_BODY.strip().splitlines()[-1]
    start = skill.read_text(encoding="utf-8").index(quote)
    db = get_db()
    try:
        artifact_id = db.execute(
            "SELECT id FROM artifact_identities WHERE path=?", (str(skill),)
        ).fetchone()[0]
    finally:
        db.close()
    suggestion, _ = comments.create_with_status(
        artifact_id, body="", author="Suggestion author", author_kind="user",
        author_user_id="persisted-suggestion-person", anchor_text=quote,
        from_off=start, to_off=start + len(quote), replacement=quote + " Now clearer.",
    )

    accepted = _user_decide(integration_client, suggestion["id"], "accept")

    assert accepted.status_code == 200, accepted.text
    relative = f".threads/{owner}/skills/demo/SKILL.md"
    author = _git(["log", "-1", "--format=%ae", "--", relative], cwd=integration_workspace).strip()
    assert author == "persisted-suggestion-person@users.corsia.local"
