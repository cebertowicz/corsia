"""GET /api/email-drafts/{artifact_id}/context: what the draft view shows, read from the draft's own Gmail account.

The draft is saved by the real ``email_draft`` tool against the ``MockTransport`` Gmail of ``test_google_email``;
the route is called as the signed-in person would reach it, past the shared person-session dependency.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import HTTPException

import infra.config as config
from api.email_drafts import draft_context
from infra.database import get_db
from registry.service import register_write
from tests.test_google_email import (  # noqa: F401  (fixtures)
    GMAIL,
    THREADED,
    _draft,
    _row,
    client_file,
    conductor,
    google,
    mail,
)


def _text(body: str) -> dict:
    return {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}


def _email(mid: str, sender: str, when_ms: int, body: str, *, labels=(), to: str = "", cc: str = "") -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": "Lunch plans"}]
    headers += [{"name": "To", "value": to}] if to else []
    headers += [{"name": "Cc", "value": cc}] if cc else []
    return {"id": mid, "threadId": "t-9", "internalDate": str(when_ms), "labelIds": list(labels),
            "payload": {**_text(body), "headers": headers}}


ANSWERED = {**THREADED, "payload": {**_text("Noon or one?"), "headers": [
    *THREADED["payload"]["headers"],
    {"name": "To", "value": "Sam Lee <sam@example.com>"}, {"name": "Cc", "value": "Ana Ruiz <ana@mail.example>"},
]}}
OLDER = _email("m-090", "Sam Lee <sam@example.com>", 1793000000000, "Lunch this week?", labels=("SENT",))
NEWER = _email("m-110", "Lee Park <lee@mail.example>", 1793100000000, "I can come too.")


def _owner(thread_id: str) -> dict:
    return {"id": get_db().execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()["user_id"]}


def _artifact(saved: dict) -> str:
    """The draft file's artifact, registered the way a tool's file write registers it."""
    path = str((config.WORKSPACE / saved["path"]).resolve())
    thread_id = get_db().execute("SELECT thread_id FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()[0]
    registered = register_write(thread_id, path, notify=False)
    assert registered is not None, path
    return registered["id"]


def _serve(mail, thread: list[dict], *, failing: frozenset[str] = frozenset()) -> None:
    """Gmail answers with this thread from now on; a listed message id fails with a 500."""
    inner = mail.fake.handler
    by_id = {item["id"]: item for item in thread}

    def handle(request: httpx.Request):
        url = str(request.url).split("?")[0]
        if url == f"{GMAIL}/threads/t-9":
            return httpx.Response(200, json={"messages": thread})
        mid = url.rsplit("/", 1)[1]
        if url.startswith(f"{GMAIL}/messages/") and mid in failing:
            return httpx.Response(500, json={})
        if url.startswith(f"{GMAIL}/messages/") and mid in by_id:
            return httpx.Response(200, json=by_id[mid])
        return inner(request)

    mail.fake.handler = handle


@pytest.mark.asyncio
async def test_a_reply_draft_reads_its_conversation_newest_first_with_the_answered_emails_addresses(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    _serve(mail, [OLDER, ANSWERED, NEWER])
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    assert context["status"] == "ready" and context["reason"] is None and context["unreadable"] == 0
    assert [m["id"] for m in context["conversation"]] == ["m-110", "m-100", "m-090"]
    assert context["replied_to"] == "m-100"
    assert [m["body"] for m in context["conversation"]] == ["I can come too.", "Noon or one?", "Lunch this week?"]
    assert context["conversation"][1]["sender"] == "Pat Kim <pat@mail.example>"
    assert context["answered"] == ["pat@mail.example", "sam@example.com", "ana@mail.example"]
    # Only quiet notes live in the record: no newer-email or recipient note, no 30-day count.
    assert all("emails from" not in note and "wrote again" not in note for note in context["notes"])
    assert context["attachments"] == []


@pytest.mark.asyncio
async def test_an_email_that_cant_be_read_is_counted_and_the_rest_still_show(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    _serve(mail, [OLDER, ANSWERED, NEWER], failing=frozenset({"m-090"}))
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    assert context["status"] == "partial" and context["unreadable"] == 1
    assert [m["id"] for m in context["conversation"]] == ["m-110", "m-100"]


@pytest.mark.asyncio
async def test_a_failed_thread_read_is_the_named_unreadable_state(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    inner = mail.fake.handler
    mail.fake.handler = lambda request: (httpx.Response(500, json={}) if str(request.url).startswith(f"{GMAIL}/threads/")
                                         else inner(request))
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    assert (context["status"], context["reason"], context["conversation"]) == ("error", "unreadable", [])
    assert context["replied_to"] == "m-100"


@pytest.mark.asyncio
async def test_a_draft_whose_account_is_gone_is_the_named_account_state(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    db = get_db()
    target = json.loads(_row(saved["draft_id"])["target_json"])
    db.execute("UPDATE action_drafts SET target_json=? WHERE id=?",
               (json.dumps({**target, "account_id": "no-such-account"}), saved["draft_id"]))
    db.commit()
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    assert (context["status"], context["reason"]) == ("error", "account")


@pytest.mark.asyncio
async def test_a_new_email_reads_nothing_from_the_account(mail):
    saved = await _draft(mail)
    before = len(mail.requests)
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    assert (context["status"], context["replied_to"], context["conversation"]) == ("ready", None, [])
    assert len(mail.requests) == before


@pytest.mark.asyncio
async def test_attachments_come_from_the_record_and_a_moved_file_is_unavailable(mail):
    from tools.reviewed_drafts import working_root

    root = working_root(mail.thread)
    root.mkdir(parents=True, exist_ok=True)
    (root / "map.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (root / "waiver.pdf").write_bytes(b"%PDF-1.4\n")
    saved = await _draft(mail, attachments=["map.png", "waiver.pdf"])
    (root / "waiver.pdf").unlink()
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    assert [(a["name"], a["type"], a["unavailable"]) for a in context["attachments"]] == [
        ("map.png", "image", False), ("waiver.pdf", "document", True)]
    assert context["attachments"][0]["path"] == str((root / "map.png").resolve())


@pytest.mark.asyncio
async def test_someone_elses_draft_and_a_plain_document_are_not_found(mail):
    saved = await _draft(mail)
    with pytest.raises(HTTPException) as other:
        await draft_context(_artifact(saved), {"id": "someone-else"})
    assert other.value.status_code == 404
    with pytest.raises(HTTPException) as missing:
        await draft_context("no-such-artifact", _owner(mail.thread))
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_the_document_read_names_a_draft_an_email_draft_and_any_other_document_a_document(mail):
    from api.files_load import read_document

    saved = await _draft(mail)
    user = _owner(mail.thread)["id"]
    draft = read_document(str(config.WORKSPACE / saved["path"]), user, None, mail.thread)
    assert draft["presentation"] == {"label": "Email draft", "icon": "mail"}
    note = config.WORKSPACE / ".threads" / mail.thread / "working" / "notes.md"
    note.write_text("plain\n", encoding="utf-8")
    assert read_document(str(note), user, None, mail.thread)["presentation"] == {"label": "Document", "icon": "file-text"}
