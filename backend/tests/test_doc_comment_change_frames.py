"""Every comment row change tells open canvases, so another canvas cannot go
stale because the write came through a different adapter.

The frame is ``doc_comment_changed`` from ``comment_notifications``. Store
writes announce after commit, except browser user creates: they suppress the
creation announcement and post one frame after their post-response batch
delivery closes. Anchor position saves and feedback send, leave, return and
retry are not comment row writes and are not covered here.
"""

from __future__ import annotations

import json

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _patch, _post, _registrable_roots, doc
from test_doc_suggestions import QUOTE, _own, _own_in_folder, _suggest, _tool_decide

pytestmark = pytest.mark.integration


@pytest.fixture
def frames(monkeypatch):
    """Artifact ids the one notifier was asked to announce, in order."""
    from doceditor import comment_notifications

    seen: list[str] = []
    monkeypatch.setattr(
        comment_notifications, "broadcast_doc_comment_changed",
        lambda artifact_id, **_kwargs: seen.append(artifact_id),
    )
    return seen


def _user_comment(client, artifact_id, **fields):
    return client.post(
        f"/api/artifacts/{artifact_id}/comments", params={"token": _OWNER},
        json={"body": "Tighten this.", "anchor_text": QUOTE, "from_off": 0, "to_off": len(QUOTE), **fields},
    )


@pytest.mark.asyncio
async def test_user_create_patch_and_decision_each_send_one_frame(
    integration_client, doc, _registrable_roots, frames,
):
    owner, artifact_id = await _own(integration_client, doc)

    created = _user_comment(integration_client, artifact_id)
    assert created.status_code == 200, created.text
    assert frames == [artifact_id]

    comment_id = created.json()["comment"]["id"]
    patched = integration_client.patch(
        f"/api/comments/{comment_id}", params={"token": _OWNER}, json={"resolved": True},
    )
    assert patched.status_code == 200, patched.text
    assert frames == [artifact_id] * 2

    suggester = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(suggester, doc)
    del frames[:]
    decided = integration_client.post(
        f"/api/comments/{suggestion['id']}/decision", params={"token": _OWNER},
        json={"decision": "reject"},
    )
    assert decided.status_code == 200, decided.text
    assert frames == [artifact_id]


@pytest.mark.asyncio
async def test_a_person_comment_announces_once_after_post_response_delivery(
    integration_client, doc, _registrable_roots, monkeypatch,
):
    """The sole user-comment frame follows background delivery, never the saved row."""
    from doceditor import comment_notifications
    from infra.database import get_db

    _owner, artifact_id = await _own(integration_client, doc)
    open_at_frame: list[int] = []


    def frame(frame_artifact_id, **_kwargs):
        db = get_db()
        try:
            open_at_frame.append(
                db.execute(
                    "SELECT COUNT(*) FROM doc_feedback_batches WHERE artifact_id=? AND state='open'", (frame_artifact_id,),
                ).fetchone()[0]
            )
        finally:
            db.close()

    monkeypatch.setattr(comment_notifications, "broadcast_doc_comment_changed", frame)
    created = _user_comment(integration_client, artifact_id)
    assert created.status_code == 200, created.text
    assert created.json()["woken"] == []
    assert open_at_frame == [0]


@pytest.mark.asyncio
async def test_refused_user_writes_send_no_frame(integration_client, doc, _registrable_roots, frames):
    _owner, artifact_id = await _own(integration_client, doc)

    empty = _user_comment(integration_client, artifact_id, body="   ")
    missing = integration_client.patch(
        "/api/comments/no-such-comment", params={"token": _OWNER}, json={"resolved": True},
    )
    assert (empty.status_code, missing.status_code) == (400, 404)
    assert frames == []


@pytest.mark.asyncio
async def test_native_tool_create_update_and_decision_each_send_one_frame(
    integration_client, doc, _registrable_roots, frames,
):
    from tools.documents import DocCommentCreateTool, DocCommentUpdateTool

    owner, artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")

    created = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=QUOTE, body="Check this.", _thread_id=reviewer,
    ))
    assert frames == [artifact_id]

    updated = json.loads(await DocCommentUpdateTool().execute(
        comment_id=created["comment"]["id"], acked=True, _thread_id=owner,
    ))
    assert updated["comment"]["acked"] == 1
    assert frames == [artifact_id] * 2

    suggestion = await _suggest(reviewer, doc)
    del frames[:]
    rejected = json.loads(await _tool_decide(owner, suggestion["id"], "reject"))
    assert rejected["comment"]["decision"] == "rejected"
    assert frames == [artifact_id]

    await _suggest(reviewer, doc, replacement="Another tail sentence")
    again = await _suggest(reviewer, doc, replacement="Document heading", quote="# Design")
    del frames[:]
    await DocCommentUpdateTool().execute(
        comment_id=again["id"], decision="reject", all_from_author=True, _thread_id=owner,
    )
    # One frame per row decided; the canvas loads once more for the burst, not once per frame.
    assert frames == [artifact_id] * 2

    missing = await DocCommentUpdateTool().execute(
        comment_id="no-such-comment", acked=True, _thread_id=owner,
    )
    assert missing == "Error: Comment not found"
    assert frames == [artifact_id] * 2


@pytest.mark.asyncio
async def test_accepting_sends_a_frame_per_recorded_row_and_none_when_nothing_is_recorded(
    integration_client, doc, _registrable_roots, frames,
):
    from doceditor.comment_actions import STALE_MESSAGE
    from tools.documents import DocCommentUpdateTool

    owner, doc = await _own_in_folder(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")

    single = await _suggest(reviewer, doc, replacement="A revised tail sentence")
    artifact_id = single["artifact_id"]
    del frames[:]
    accepted = json.loads(await _tool_decide(owner, single["id"], "accept"))
    assert accepted["comment"]["decision"] == "accepted"
    assert frames == [artifact_id]

    first = await _suggest(reviewer, doc, replacement="A third tail sentence", quote="A revised tail sentence")
    second = await _suggest(reviewer, doc, replacement="Document heading", quote="# Design")
    del frames[:]
    batch = json.loads(await DocCommentUpdateTool().execute(
        comment_id=first["id"], decision="accept", all_from_author=True, _thread_id=owner,
    ))
    assert set(batch["accepted"]) == {first["id"], second["id"]}
    assert frames == [artifact_id] * 2

    stale = await _suggest(reviewer, doc, replacement="A fourth tail sentence", quote="A third tail sentence")
    doc.write_text(doc.read_text().replace("A third tail sentence", "Something else entirely"))
    del frames[:]
    assert await _tool_decide(owner, stale["id"], "accept") == f"Error: {STALE_MESSAGE}"
    skipped = json.loads(await DocCommentUpdateTool().execute(
        comment_id=stale["id"], decision="accept", all_from_author=True, _thread_id=owner,
    ))
    assert (skipped["accepted"], [row["id"] for row in skipped["skipped"]]) == ([], [stale["id"]])
    assert frames == []


def test_a_rolled_back_comment_write_sends_no_frame(integration_client, doc, _registrable_roots, frames):
    from doceditor import comments as store
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with pytest.raises(RuntimeError, match="undo"):
            with write_transaction(db, scope="test_rolled_back_comment") as connection:
                store.create("artifact-never-committed", body="gone", db=connection)
                raise RuntimeError("undo")
        assert frames == []
        with write_transaction(db, scope="test_committed_comment") as connection:
            store.create("artifact-committed", body="kept", db=connection)
            assert frames == []
        assert frames == ["artifact-committed"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_frame_announced_off_the_event_loop_is_sent_on_it(monkeypatch):
    """A database worker commits the write; a send made there would be dropped."""
    import asyncio
    import threading

    import infra.app_loop as app_loop
    from doceditor import comment_notifications

    from infra.state import ws as sockets

    sent: list[tuple[dict, int]] = []
    arrived = asyncio.Event()

    class Socket:
        async def send_json(self, message: dict) -> None:
            sent.append((message, threading.get_ident()))
            arrived.set()

    monkeypatch.setattr(comment_notifications, "_comment_change_audience", lambda _id, db=None: "user-1")
    monkeypatch.setitem(sockets._ws_user_clients, "user-1", [Socket()])
    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())
    await asyncio.to_thread(comment_notifications.broadcast_doc_comment_changed, "artifact-1")
    await asyncio.wait_for(arrived.wait(), timeout=5)
    assert sent == [
        ({"type": "doc_comment_changed", "artifact_id": "artifact-1"}, threading.get_ident()),
    ]


@pytest.mark.asyncio
async def test_a_stored_comment_reaches_the_socket_send_through_the_whole_chain(
    integration_client, doc, _registrable_roots, monkeypatch,
):
    """Nothing stubbed between the store and the socket: the write commits on a
    database worker, the audience is read from the real rows there, and the
    socket write runs on the application loop."""
    import asyncio
    import threading

    import infra.app_loop as app_loop
    from doceditor import comment_notifications, comments as store
    from infra.database import write_transaction
    from infra.db_async import run_in_db

    _owner, artifact_id = await _own(integration_client, doc)
    from infra.state import ws as sockets

    sent: list[tuple[dict, int]] = []
    arrived = asyncio.Event()

    class Socket:
        async def send_json(self, message: dict) -> None:
            if message.get("type") != "doc_comment_changed":
                return
            sent.append((message, threading.get_ident()))
            arrived.set()

    audience = comment_notifications._comment_change_audience(artifact_id)
    assert audience
    # One open socket for whoever the frame is for: every client, or the owner.
    monkeypatch.setattr(sockets, "_ws_thread_clients", {})
    monkeypatch.setattr(
        sockets, "_ws_user_clients", {"viewer" if audience == "all" else audience: [Socket()]},
    )

    def write():
        with write_transaction(None, scope="test_comment_whole_chain") as connection:
            store.create(artifact_id, body="Stored on a worker.", db=connection)
        return threading.get_ident()

    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())
    worker = await run_in_db(write)
    await asyncio.wait_for(arrived.wait(), timeout=5)

    assert worker != threading.get_ident()
    assert sent == [({"type": "doc_comment_changed", "artifact_id": artifact_id}, threading.get_ident())]


@pytest.mark.asyncio
async def test_cli_route_decision_sends_one_frame_not_two(
    integration_client, doc, _registrable_roots, frames,
):
    owner, artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _suggest(reviewer, doc)
    del frames[:]

    decided = _patch(integration_client, _OWNER, suggestion["id"], decision="reject", as_session_id=owner)
    assert decided.status_code == 200, decided.text
    assert frames == [artifact_id]

    top = _post(integration_client, _OWNER, doc_path=str(doc), quote=QUOTE, body="top", as_session_id=reviewer)
    assert top.status_code == 200, top.text
    assert frames == [artifact_id] * 2
