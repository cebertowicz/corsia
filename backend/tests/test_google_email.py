"""email_draft and email_send: the draft document, the gate in front of the send, and what a
failed send proves.

Google is a ``MockTransport``. The gate's own rules are pinned in ``test_action_gate``; here the
real gate is used where it refuses (no live turn) and a pass-through stands in where the test is
about what the send tool does after approval.
"""

from __future__ import annotations

import base64
import json
import logging
from email import message_from_bytes, policy
from pathlib import Path

import httpx
import pytest

from connections.google import client as google_client
from connections.google import email_document
from doceditor.document_scope import revision as document_revision
import infra.config as config
from infra.database import get_db
from tests.test_google_sign_in import (  # noqa: F401  (fixtures)
    ACCESS,
    EMAIL,
    REFRESH,
    SECRET,
    _http,
    _start,
    client_file,
    conductor,
    google,
)
from tools import action_gate, google_email
from tools.action_drafts import claim_for_send, create_draft, mark_sent, record_draft
from tools.action_gate import Approved
from tools.google_email import EmailDraftTool, EmailSendTool

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SIGNATURE_HTML = "<div>Sam Lee<br>CORSIA &amp; Co</div><div><br></div>"
THREADED = {
    "id": "m-100", "threadId": "t-9",
    "snippet": "SECRET BODY SNIPPET",
    "payload": {"headers": [
        {"name": "Message-ID", "value": "<abc@mail.example>"},
        {"name": "References", "value": "<root@mail.example>"},
        {"name": "From", "value": "Pat Kim <pat@mail.example>"},
        {"name": "Subject", "value": "Lunch plans"},
    ]},
}


class Gmail:
    """The Gmail endpoints these tools use; records every request."""

    def __init__(self, fake, thread: str, workspace: Path) -> None:
        self.fake = fake
        self.thread = thread
        self.workspace = workspace
        self.requests: list[httpx.Request] = []
        self.sends: list[dict] = []
        self.signature_status = 200
        self.send_mode = "ok"
        self.messages = {"m-100": THREADED}
        inner = fake.handler
        fake.handler = lambda request: self._handle(request, inner)

    def _handle(self, request: httpx.Request, inner):
        url = str(request.url).split("?")[0]
        if url == google_client.TOKEN_URL:
            return inner(request)
        self.requests.append(request)
        if url == f"{GMAIL}/settings/sendAs":
            if self.signature_status != 200:
                return httpx.Response(self.signature_status, json={})
            return httpx.Response(200, json={"sendAs": [
                {"sendAsEmail": EMAIL, "isPrimary": True, "signature": SIGNATURE_HTML},
            ]})
        if url.startswith(f"{GMAIL}/messages/") and request.method == "GET":
            found = self.messages.get(url.rsplit("/", 1)[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if url == f"{GMAIL}/messages/send":
            self.sends.append(json.loads(request.content))
            mode = self.send_mode
            if mode == "connect":
                raise httpx.ConnectError("no route", request=request)
            if mode == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            if mode == "400":
                return httpx.Response(400, json={"error": "bad"})
            if mode == "500":
                return httpx.Response(500, json={})
            if mode == "noid":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"id": "sent-1", "threadId": "t-9"})
        return httpx.Response(404, json={})

    def sent_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).endswith("/messages/send")]


@pytest.fixture
async def mail(google, conductor, client_file, tmp_path, monkeypatch):
    from infra.platform_paths import WorkspaceSelection
    from registry import service
    from tools import sandbox_config

    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="workspace-stable-id", root=workspace, name="Test workspace"),
    )
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **_kwargs: [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    _, state = await _start(google, conductor, client_file)
    async with _http() as http:
        response = await http.get("/api/google/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 200
    return Gmail(google, conductor, workspace)


@pytest.fixture
def approve(monkeypatch):
    """Stand in for an approving gate: the thread's draft row and the file's current bytes."""

    async def approved(kind, draft_id, revision):
        row = get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
        return Approved(dict(row), (config.WORKSPACE / row["path"]).read_bytes())

    monkeypatch.setattr(google_email, "check_action", approved)


async def _draft(mail: Gmail, **fields) -> dict:
    args = {"to": ["sam@example.com"], "subject": "Lunch", "body": "Noon works?", **fields}
    out = await EmailDraftTool().execute(_thread_id=mail.thread, **args)
    assert not out.startswith("Error"), out
    return json.loads(out)


def _row(draft_id: str) -> dict:
    return dict(get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())


async def _send(mail: Gmail, saved: dict) -> str:
    return await EmailSendTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=saved["revision"],
    )


def _mime(body: dict):
    return message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)


# --- email_draft ---------------------------------------------------------------------------


async def test_draft_writes_a_document_with_the_signature_and_records_its_bytes(mail):
    saved = await _draft(mail, cc=["pat@example.com"])
    assert set(saved) == {"draft_id", "revision", "path", "from", "next"}
    assert saved["from"] == EMAIL
    document = mail.workspace / saved["path"]
    assert f".threads/{mail.thread}/working/email-drafts/" in str(document)
    text = document.read_text()
    assert text == (
        f"From: {EMAIL}\nTo: sam@example.com\nCc: pat@example.com\nSubject: Lunch\n\n"
        "Noon works?\n\n-- \nSam Lee\nCORSIA & Co\n"
    )
    row = _row(saved["draft_id"])
    assert row["state"] == "open" and row["kind"] == "email" and row["thread_id"] == mail.thread
    assert row["path"] == saved["path"]
    assert row["recorded_revision"] == saved["revision"] == document_revision(document.read_bytes())


async def test_update_rewrites_the_same_document_and_re_records_it(mail):
    first = await _draft(mail, reply_to_message_id="m-100")
    second = await _draft(mail, draft_id=first["draft_id"], body="Noon at the usual place?")
    assert second["draft_id"] == first["draft_id"] and second["path"] == first["path"]
    assert second["revision"] != first["revision"]
    assert "usual place" in (mail.workspace / second["path"]).read_text()
    row = _row(first["draft_id"])
    assert row["recorded_revision"] == second["revision"]
    # A reply target set earlier survives an update that does not name one.
    assert json.loads(row["target_json"])["thread_id"] == "t-9"
    count = get_db().execute("SELECT COUNT(*) FROM action_drafts").fetchone()[0]
    assert count == 1


async def test_reply_reads_message_headers_only(mail):
    saved = await _draft(mail, reply_to_message_id="m-100")
    reads = [r for r in mail.requests if r.method == "GET" and "/messages/m-100" in str(r.url)]
    assert len(reads) == 1
    params = reads[0].url.params
    assert params["format"] == "metadata"
    assert sorted(params.get_list("metadataHeaders")) == ["From", "Message-ID", "References", "Subject"]
    target = json.loads(_row(saved["draft_id"])["target_json"])
    assert target == {
        "reply_to": "m-100", "thread_id": "t-9", "in_reply_to": "<abc@mail.example>",
        "references": "<root@mail.example> <abc@mail.example>",
    }
    assert "SECRET BODY" not in (mail.workspace / saved["path"]).read_text()


async def test_a_reply_target_is_shown_in_the_document_and_a_plain_draft_shows_none(mail):
    plain = await _draft(mail)
    assert "In reply to:" not in (mail.workspace / plain["path"]).read_text()

    saved = await _draft(mail, reply_to_message_id="m-100")
    head = (mail.workspace / saved["path"]).read_text().split("\n\n")[0].split("\n")
    assert head[-2:] == ["Subject: Lunch", 'In reply to: "Lunch plans" from Pat Kim <pat@mail.example>']
    # Display only: the send threads with the stored target, and the line is not a send input.
    assert json.loads(_row(saved["draft_id"])["target_json"])["reply_to"] == "m-100"
    assert email_document.parse_document((mail.workspace / saved["path"]).read_text())["subject"] == "Lunch"
    # An update that names no target keeps threading, so the document keeps saying so.
    again = await _draft(mail, draft_id=saved["draft_id"], body="Later?")
    assert "In reply to: " in (mail.workspace / again["path"]).read_text()


async def test_a_reply_target_with_no_readable_headers_is_still_shown_and_saved(mail):
    mail.messages["m-100"] = {"id": "m-100", "threadId": "t-9", "payload": {"headers": []}}
    saved = await _draft(mail, reply_to_message_id="m-100")
    assert "\nIn reply to: an earlier message\n" in (mail.workspace / saved["path"]).read_text()


async def test_message_ids_are_url_encoded_into_a_fixed_template(mail):
    out = await EmailDraftTool().execute(
        _thread_id=mail.thread, to=["a@example.com"], subject="s", body="b",
        reply_to_message_id="../settings/sendAs?x=1",
    )
    assert out == "Error: That message can't be found."
    url = str(mail.requests[-1].url)
    assert url.startswith(f"{GMAIL}/messages/..%2Fsettings%2FsendAs%3Fx%3D1")


@pytest.mark.parametrize(("fields", "text"), [
    ({"to": []}, "A draft needs at least one recipient."),
    ({"to": ["Sam <sam@example.com>"]}, "Recipients must be plain email addresses such as name@example.com."),
    ({"to": ["sam@example.com\nBcc: x@example.com"]},
     "Recipients must be plain email addresses such as name@example.com."),
    ({"cc": ["nobody"]}, "Recipients must be plain email addresses such as name@example.com."),
    ({"subject": "  "}, "A draft needs a one-line subject."),
    ({"subject": "Hi\nBcc: x@example.com"}, "A draft needs a one-line subject."),
    ({"body": ""}, "A draft needs a body."),
])
async def test_draft_refuses_bad_input_and_writes_nothing(mail, fields, text):
    args = {"to": ["sam@example.com"], "subject": "Lunch", "body": "Noon?", **fields}
    out = await EmailDraftTool().execute(_thread_id=mail.thread, **args)
    assert out == f"Error: {text}"
    assert get_db().execute("SELECT COUNT(*) FROM action_drafts").fetchone()[0] == 0
    assert not list(mail.workspace.rglob("email-*.md"))


async def test_draft_fails_rather_than_dropping_a_signature_it_could_not_fetch(mail):
    mail.signature_status = 500
    out = await EmailDraftTool().execute(
        _thread_id=mail.thread, to=["sam@example.com"], subject="s", body="b",
    )
    assert out.startswith("Error: The draft was not saved.")
    assert get_db().execute("SELECT COUNT(*) FROM action_drafts").fetchone()[0] == 0
    assert not list(mail.workspace.rglob("email-*.md"))


async def test_update_needs_an_open_draft_of_this_thread(mail):
    saved = await _draft(mail)
    args = {"to": ["sam@example.com"], "subject": "s", "body": "b"}
    out = await EmailDraftTool().execute(_thread_id=mail.thread, draft_id="nope", **args)
    assert out == "Error: There is no open draft with that id in this conversation."
    claim_for_send(saved["draft_id"])
    mark_sent(saved["draft_id"], "x")
    out = await EmailDraftTool().execute(_thread_id=mail.thread, draft_id=saved["draft_id"], **args)
    assert out == "Error: There is no open draft with that id in this conversation."


async def test_both_tools_belong_to_the_designated_thread_only(mail):
    from tests.registry_helpers import make_session

    other = make_session(get_db(), prefix="other")
    refused = "Error: Only the designated CORSIA thread can use Google tools."
    out = await EmailDraftTool().execute(_thread_id=other, to=["a@example.com"], subject="s", body="b")
    assert out == refused
    assert await EmailSendTool().execute(_thread_id=other, draft_id="d", revision="r") == refused


# --- email_send ----------------------------------------------------------------------------


async def test_send_without_a_live_turn_sends_nothing(mail):
    saved = await _draft(mail)
    out = await _send(mail, saved)
    assert out == f"Error: {action_gate.NO_TURN}"
    assert mail.sent_requests() == []
    assert _row(saved["draft_id"])["state"] == "open"


async def test_send_builds_the_message_from_the_approved_bytes_and_marks_it_sent(mail, approve):
    saved = await _draft(mail, cc=["pat@example.com"], reply_to_message_id="m-100")
    out = json.loads(await _send(mail, saved))
    assert out == {"sent_message_id": "sent-1", "thread_id": "t-9"}
    [request] = mail.sent_requests()
    assert request.headers["authorization"] == f"Bearer {ACCESS}"
    body = mail.sends[0]
    assert body["threadId"] == "t-9"
    message = _mime(body)
    assert message["To"] == "sam@example.com" and message["Cc"] == "pat@example.com"
    assert message["Subject"] == "Lunch"
    assert message["In-Reply-To"] == "<abc@mail.example>"
    assert message["References"] == "<root@mail.example> <abc@mail.example>"
    assert message.get_content_type() == "text/plain"
    document = (mail.workspace / saved["path"]).read_text()
    expected = document.partition("\n\n")[2]
    assert message.get_content().replace("\r\n", "\n").rstrip("\n") == expected.rstrip("\n")
    row = _row(saved["draft_id"])
    assert row["state"] == "sent" and row["external_id"] == "sent-1" and row["sent_at"]


async def test_the_message_carries_the_bytes_the_gate_approved_not_a_second_read(mail, monkeypatch):
    saved = await _draft(mail, body="Approved text.")
    approved_text = (mail.workspace / saved["path"]).read_bytes()
    (mail.workspace / saved["path"]).write_text("To: evil@example.com\nSubject: Swapped\n\nSwapped text.\n")

    async def gate(kind, draft_id, revision):
        return Approved(_row(draft_id), approved_text)

    monkeypatch.setattr(google_email, "check_action", gate)
    await _send(mail, saved)
    message = _mime(mail.sends[0])
    assert message["To"] == "sam@example.com" and message["Subject"] == "Lunch"
    assert "Approved text." in message.get_content() and "Swapped" not in message.get_content()


async def test_a_send_that_never_left_returns_the_draft_to_open(mail, approve):
    for mode in ("connect", "400"):
        mail.send_mode = mode
        saved = await _draft(mail, subject=f"Lunch {mode}")
        out = await _send(mail, saved)
        assert out.startswith("Error: Not sent:"), out
        assert _row(saved["draft_id"])["state"] == "open"
    # Once Google is reachable again the same draft goes out.
    mail.send_mode = "ok"
    assert json.loads(await _send(mail, saved))["sent_message_id"] == "sent-1"


async def test_a_sign_in_google_refuses_before_the_send_leaves_the_draft_open(mail, approve):
    saved = await _draft(mail)
    mail.fake.refresh_status = 400
    out = await _send(mail, saved)
    assert out.startswith("Error: Not sent: Google rejected the saved sign-in.")
    assert mail.sent_requests() == []
    assert _row(saved["draft_id"])["state"] == "open"


@pytest.mark.parametrize("mode", ["timeout", "500", "noid"])
async def test_a_send_whose_outcome_is_unknown_stays_claimed_and_is_not_repeated(mail, approve, mode):
    mail.send_mode = mode
    saved = await _draft(mail)
    out = await _send(mail, saved)
    assert out == f"Error: {google_email.UNCONFIRMED}"
    assert _row(saved["draft_id"])["state"] == "sending"
    mail.send_mode = "ok"
    again = await _send(mail, saved)
    assert again == f"Error: {google_email.STILL_SENDING}"
    assert len(mail.sent_requests()) == 1


async def test_a_lost_claim_race_sends_nothing(mail, approve, monkeypatch):
    saved = await _draft(mail)

    def lost(draft_id, **kwargs):
        from tools.action_drafts import DraftError

        raise DraftError("The draft is not open.")

    monkeypatch.setattr(google_email, "claim_for_send", lost)
    assert await _send(mail, saved) == f"Error: {google_email.STILL_SENDING}"
    assert mail.sent_requests() == []


async def test_a_from_line_that_is_not_the_connected_account_sends_nothing(mail, approve):
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    path.write_text(path.read_text().replace(f"From: {EMAIL}", "From: someone.else@example.com"))
    out = await _send(mail, saved)
    assert out == (
        f"Error: Not sent: the From line doesn't match the connected Google account ({EMAIL}). "
        "Save the draft again with email_draft and show the person."
    )
    assert mail.sent_requests() == []
    assert _row(saved["draft_id"])["state"] == "open"


async def test_from_is_compared_without_case_and_never_chooses_the_sender(mail, approve):
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    path.write_text(path.read_text().replace(f"From: {EMAIL}", f"From: {EMAIL.upper()}"))
    assert json.loads(await _send(mail, saved))["sent_message_id"] == "sent-1"
    assert "from" not in {key.lower() for key in _mime(mail.sends[0]).keys()}


async def test_a_document_without_a_from_line_still_sends(mail, approve):
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    path.write_text(path.read_text().replace(f"From: {EMAIL}\n", "", 1))
    assert "From:" not in path.read_text().split("\n\n")[0]
    assert json.loads(await _send(mail, saved))["sent_message_id"] == "sent-1"


async def test_a_document_that_is_not_in_draft_format_is_refused_before_the_claim(mail, approve):
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    path.write_text("Subject: only\n\nNo recipient.\n")
    assert await _send(mail, saved) == f"Error: {google_email.BAD_DOCUMENT}"
    assert _row(saved["draft_id"])["state"] == "open"
    assert mail.sent_requests() == []


async def test_a_line_break_in_a_stored_threading_header_cannot_add_a_header(mail, approve):
    saved = await _draft(mail, reply_to_message_id="m-100")
    target = json.loads(_row(saved["draft_id"])["target_json"])
    target["in_reply_to"] = "<a@b>\r\nBcc: attacker@example.com"
    get_db().execute(
        "UPDATE action_drafts SET target_json=? WHERE id=?", (json.dumps(target), saved["draft_id"]),
    )
    get_db().commit()
    assert await _send(mail, saved) == f"Error: {google_email.BAD_DOCUMENT}"
    assert mail.sent_requests() == []


async def test_no_google_secret_reaches_a_result_or_a_log(mail, approve, caplog):
    caplog.set_level(logging.DEBUG)
    outputs = []
    saved = await _draft(mail, reply_to_message_id="m-100")
    outputs.append(json.dumps(saved))
    outputs.append(await _send(mail, saved))
    mail.send_mode = "500"
    again = await _draft(mail, subject="Second")
    outputs.append(await _send(mail, again))
    third = await _draft(mail, subject="Third")
    mail.fake.refresh_status = 400
    outputs.append(await _send(mail, third))
    blob = "\n".join(outputs) + caplog.text
    for secret in (ACCESS, REFRESH, SECRET):
        assert secret not in blob


# --- client, document and gate helpers ------------------------------------------------------


@pytest.mark.parametrize(("failure", "never_sent"), [
    (lambda r: httpx.ConnectError("x", request=r), True),
    (lambda r: httpx.ConnectTimeout("x", request=r), True),
    (lambda r: httpx.ReadTimeout("x", request=r), False),
    (lambda r: httpx.RemoteProtocolError("x", request=r), False),
    (lambda r: httpx.Response(404, json={}), True),
    (lambda r: httpx.Response(500, json={}), False),
    (lambda r: httpx.Response(200, content=b"not json"), False),
])
async def test_the_client_says_when_a_request_provably_never_took_effect(
    google, monkeypatch, failure, never_sent,
):
    from infra import key_store

    monkeypatch.setattr(key_store, "resolve_google", lambda: {})

    def handler(request):
        outcome = failure(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    google.handler = handler
    with pytest.raises(google_client.GoogleRequestFailed) as caught:
        await google_client.request_json("POST", f"{GMAIL}/messages/send", body={}, token="canary-bearer")
    assert caught.value.never_sent is never_sent


def test_signature_html_becomes_plain_text():
    html = "<div dir=ltr>--<br>Sam&nbsp;Lee<br><b>CORSIA</b> &amp; Co</div><style>p{}</style><div><br></div>"
    assert email_document.signature_text(html) == "Sam Lee\nCORSIA & Co"


def test_a_document_round_trips_and_keeps_the_body_exactly():
    text = email_document.compose_document(
        "me@example.com", ["a@example.com", "b@example.com"], ["c@example.com"], "Hi", "Line one.\n\nLine two.", "Sam",
    )
    parsed = email_document.parse_document(text)
    assert parsed["from"] == "me@example.com"
    assert parsed["to"] == ["a@example.com", "b@example.com"] and parsed["cc"] == ["c@example.com"]
    assert parsed["subject"] == "Hi"
    assert parsed["body"] == "Line one.\n\nLine two.\n\n-- \nSam\n"


@pytest.mark.parametrize("text", [
    "",
    "To: a@example.com\nSubject: s\nno blank line",
    "From: a@example.com\nFrom: b@example.com\nTo: c@example.com\nSubject: s\n\nbody\n",
    "To: a@example.com\nBcc: b@example.com\nSubject: s\n\nbody\n",
    "To: a@example.com\nTo: b@example.com\nSubject: s\n\nbody\n",
    "To: a@example.com\n\nbody\n",
    "To: Sam <a@example.com>\nSubject: s\n\nbody\n",
    "To: a@example.com\nSubject: s\n\n  \n",
])
def test_a_document_outside_the_format_does_not_parse(text):
    assert email_document.parse_document(text) is None


def test_record_draft_replaces_the_target_only_when_given(isolated_db):
    saved = create_draft("t1", "email", "d.md", b"one", {"reply_to": "m1"})
    record_draft(saved["draft_id"], b"two")
    assert json.loads(_row(saved["draft_id"])["target_json"]) == {"reply_to": "m1"}
    record_draft(saved["draft_id"], b"three", {"reply_to": "m2"})
    row = _row(saved["draft_id"])
    assert json.loads(row["target_json"]) == {"reply_to": "m2"}
    assert row["recorded_revision"] == document_revision(b"three")


async def test_the_agent_is_told_to_send_only_when_the_person_says_to(mail):
    from tools.catalog_entries import TOOL_CATALOG

    sentence = (
        "Show the person the draft (link its path), end your turn, and call email_send only when "
        "their reply says to send it; if they ask for changes, save again with email_draft and "
        "show them the new version."
    )
    assert sentence in EmailDraftTool().description
    assert "Returns draft_id and revision, which email_send needs." in EmailDraftTool().description
    assert "latest message says to send this draft" in EmailSendTool().description
    assert (await _draft(mail))["next"] == sentence
    assert TOOL_CATALOG["email_send"].summary == "Send a saved email draft after the person says to send it"
