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
UNSENT = _email("m-120", "Sam Lee <sam@example.com>", 1793200000000, "Half-written.", labels=("DRAFT",))


def _owner(thread_id: str) -> dict:
    return {"id": get_db().execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()["user_id"]}


def _artifact(saved: dict) -> str:
    """The draft file's artifact, registered the way a tool's file write registers it."""
    path = str((config.WORKSPACE / saved["path"]).resolve())
    thread_id = get_db().execute("SELECT thread_id FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()[0]
    registered = register_write(thread_id, path, notify=False)
    assert registered is not None, path
    return registered["id"]


def _serve(mail, thread: list[dict]) -> list[httpx.Request]:
    """Gmail answers ``threads.get`` with this thread from now on; returns every Gmail request it gets."""
    inner, seen = mail.fake.handler, []

    def handle(request: httpx.Request):
        if str(request.url).startswith(GMAIL):
            seen.append(request)
        if str(request.url).split("?")[0] == f"{GMAIL}/threads/t-9":
            return httpx.Response(200, json={"messages": thread})
        return inner(request)

    mail.fake.handler = handle
    return seen


@pytest.mark.asyncio
async def test_a_reply_draft_reads_its_conversation_newest_first_with_the_answered_emails_addresses(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    seen = _serve(mail, [OLDER, ANSWERED, NEWER, UNSENT])
    context = await draft_context(_artifact(saved), _owner(mail.thread))
    # One provider call reads the whole conversation; the account's own unsent drafts are left out.
    [read] = seen
    assert str(read.url).split("?")[0] == f"{GMAIL}/threads/t-9" and read.url.params["format"] == "full"
    assert context["status"] == "ready" and context["reason"] is None and "unreadable" not in context
    assert [m["id"] for m in context["conversation"]] == ["m-110", "m-100", "m-090"]
    assert context["replied_to"] == "m-100"
    assert [m["body"] for m in context["conversation"]] == ["I can come too.", "Noon or one?", "Lunch this week?"]
    assert context["conversation"][1]["sender"] == "Pat Kim <pat@mail.example>"
    assert context["answered"] == ["pat@mail.example", "sam@example.com", "ana@mail.example"]
    # Count and original read/screening evidence are retained because conversation rows do not carry those facts.
    assert context["notes"] == json.loads(_row(saved["draft_id"])["target_json"])["notes"]
    assert any("emails from" in note for note in context["notes"])
    assert any(note.startswith("The message being replied to") for note in context["notes"])
    assert context["attachments"] == []


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
async def test_a_signed_out_account_is_the_account_state_not_a_retry(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    mail.fake.refresh_status = 400
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
async def test_only_the_draft_record_makes_a_file_an_email_draft(mail):
    from api.files_load import read_document

    saved = await _draft(mail)
    user = _owner(mail.thread)["id"]
    path = config.WORKSPACE / saved["path"]
    draft = read_document(str(path), user, None, mail.thread)["email_draft"]
    assert draft["id"] == saved["draft_id"]
    assert draft["header"]["text"] == path.read_text().partition("\n\n")[0]
    assert [r["text"] for r in draft["header"]["recipients"]] == ["sam@example.com"]
    # Same folder, same format, no record: an ordinary document.
    copy = path.with_name("copy.md")
    copy.write_text(path.read_text(), encoding="utf-8")
    assert read_document(str(copy), user, None, mail.thread)["email_draft"] is None
