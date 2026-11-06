from email.message import EmailMessage
import smtplib
from types import SimpleNamespace

import pytest

from connections.mail import attachments
from connections.mail.email_document import DraftRefused


@pytest.fixture
def working_root(tmp_path, monkeypatch):
    root = tmp_path / "working"
    root.mkdir()
    monkeypatch.setattr(attachments, "working_root", lambda thread_id: root)
    def resolve(value, root, *, thread_id):
        assert thread_id == "thread"
        return (root / value).resolve()
    monkeypatch.setattr(attachments, "resolve_path", resolve)
    return root


def test_attachment_must_be_inside_thread_working_root(working_root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    with pytest.raises(DraftRefused, match="missing or unreadable"):
        attachments.inspect("thread", [str(outside)], 100)
    link = working_root / "link"
    link.symlink_to(outside)
    with pytest.raises(DraftRefused, match="missing or unreadable"):
        attachments.inspect("thread", ["link"], 100)
    with pytest.raises(DraftRefused, match="missing or unreadable"):
        attachments.add("thread", EmailMessage(), [{"path": str(outside), "size": 6}])


@pytest.mark.parametrize("operation", ["inspect", "add"])
def test_fenced_project_attachment_preserves_refusal(tmp_path, monkeypatch, operation):
    from tools import project_fence
    from tools.project_fence import Fence, FencedRoot

    private = tmp_path / "projects" / "Private"
    private.mkdir(parents=True)
    (private / "secret.txt").write_text("secret")
    monkeypatch.setattr(attachments, "working_root", lambda thread_id: tmp_path)
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda thread_id: Fence((FencedRoot(private, "Private"),), thread_id))
    monkeypatch.setattr(project_fence, "_event", lambda *args, **kwargs: None)
    with pytest.raises(DraftRefused, match="in project `Private`, outside your scope"):
        if operation == "inspect":
            attachments.inspect("workspace-agent", [str(private / "secret.txt")], 100)
        else:
            attachments.add("workspace-agent", EmailMessage(), [{"path": str(private / "secret.txt"), "size": 6}])


def test_total_limit_checked_before_reading_any_file(working_root, monkeypatch):
    (working_root / "one").write_bytes(b"123")
    (working_root / "two").write_bytes(b"456")
    def fail_open(*args, **kwargs):
        raise AssertionError("oversized files must not be read")
    monkeypatch.setattr(type(working_root), "open", fail_open)
    with pytest.raises(DraftRefused, match="Not sent: the attachments are too large"):
        attachments.inspect("thread", ["one", "two"], 5)


def test_exact_limit_and_changed_file(working_root):
    path = working_root / "one.txt"
    path.write_bytes(b"123")
    entries = attachments.inspect("thread", ["one.txt"], 3)
    message = EmailMessage()
    attachments.add("thread", message, entries)
    assert next(message.iter_attachments()).get_payload(decode=True) == b"123"
    path.write_bytes(b"456")
    with pytest.raises(DraftRefused, match="changed"):
        attachments.add("thread", EmailMessage(), entries)
    path.write_bytes(b"longer")
    with pytest.raises(DraftRefused, match="changed"):
        attachments.add("thread", EmailMessage(), entries)


def test_smtp_oversize_refusal_uses_shared_text(monkeypatch):
    from connections.account_errors import AccountError
    from connections.mailhost import smtp

    class Client:
        def send_message(self, *args, **kwargs):
            raise smtplib.SMTPDataError(552, b"too big")

        def close(self):
            pass

    monkeypatch.setattr(smtp, "logged_in_client", lambda *args: Client())
    message = EmailMessage()
    message.add_attachment(b"x", maintype="text", subtype="plain")
    with pytest.raises(AccountError) as exc:
        smtp.send_message(SimpleNamespace(), "u", "p", message, from_addr="a@example.com")
    assert f"Not sent: {exc.value}" == f"Not sent: {attachments.OVERSIZE}"


@pytest.mark.asyncio
async def test_gmail_oversize_refusal_uses_shared_text(monkeypatch):
    from connections.google.client import GoogleRequestFailed
    from tools import gmail_backend

    async def capture(*args, **kwargs):
        exc = GoogleRequestFailed(413)
        assert kwargs["never_sent"](exc)
        return f"Not sent: {kwargs['request_error'](exc)}"

    monkeypatch.setattr(gmail_backend, "deliver_claimed", capture)
    gate = SimpleNamespace(still_sending="", claim_error="", label="")
    result = await gmail_backend.GmailBackend().send_email(
        "draft", {"id": "account"}, EmailMessage(), {"attachments": [{}]}, gate,
    )
    assert result == f"Not sent: {attachments.OVERSIZE}"
