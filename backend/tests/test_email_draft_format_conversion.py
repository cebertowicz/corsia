"""Open email drafts convert through the document writer without changing their body or losing notes."""
import json
import os

import pytest

from connections.mail.email_document import parse_document
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write
from tools.action_drafts import create_draft

HEADER = "From: me@example.com\r\nTo: sam@example.com\r\nSubject: Lunch\r\n"
BODY = "\r\nDo not alter this.\r\nNote: this is body text\r\nIn reply to: also body text\r\n\r\n-- \r\nSam\r\n"
COUNT = "4 emails from Sam <sam@example.com> in the last 30 days."
OLD = HEADER + f"In reply to: m-1\r\nNote: {COUNT}\r\nNote: The sender wrote again in this thread:\r\n" + BODY


def _draft(scope, name, *, state="open", text=OLD):
    _write(scope, name, text)
    path = scope["category"] / name
    relative = os.path.relpath(path, scope["workspace"])
    saved = create_draft(scope["owner"], "email", relative, path.read_bytes(), {"account_id": "a", "notes": ["Existing"]})
    scope["db"].execute("UPDATE action_drafts SET state=? WHERE id=?", (state, saved["draft_id"]))
    scope["db"].commit()
    return path, saved["draft_id"]


@pytest.mark.asyncio
async def test_open_draft_converts_once_preserving_body_and_the_person_notes(ownership_scope):
    from connections.mail.draft_format_conversion import convert_open_drafts
    scope = ownership_scope
    path, draft_id = _draft(scope, "old.md")
    before = path.read_bytes()
    await convert_open_drafts()
    after = path.read_bytes()
    assert after == (HEADER + BODY).encode()
    assert after.partition(b"\r\n\r\n")[2] == before.partition(b"\r\n\r\n")[2]
    assert parse_document(after.decode()) is not None
    row = scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
    assert json.loads(row["target_json"])["notes"] == ["Existing", COUNT]
    first = dict(row)
    await convert_open_drafts()
    assert path.read_bytes() == after
    assert dict(scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()) == first


@pytest.mark.asyncio
async def test_old_note_lines_become_one_note_without_what_the_view_shows_elsewhere(ownership_scope):
    from connections.mail.draft_format_conversion import convert_open_drafts
    scope = ownership_scope
    # The guard's evidence (the agent gets it), the replied-to email's evidence (the conversation shows it) and
    # attachment lines (the chips show them) go, including a shown email whose lines read like kept notes.
    guard = ["Another person wrote in this thread:", "Pat wrote on Nov 2 (read).", "<untrusted-email>",
             "The earlier message from Pat will be quoted below the reply.", "</untrusted-email>",
             "The sender wrote about the same subject in another thread:", "Sam wrote on Nov 3 (unread).",
             "The text isn't shown here."]
    count = "4 emails from Sam <sam@example.com> in the last 30 days."
    stale = ["The sender wrote again in this thread:", "Sam wrote on Nov 3 (unread).",
             "<untrusted-email>Also, 2 emails from Sam in the last 30 days.</untrusted-email>"]
    shown = ["The message being replied to:", "Sam wrote on Nov 1, 2026, 9:00 AM UTC (read).",
             "<untrusted-email>Lunch?</untrusted-email>"]
    quoted = "The earlier message from Sam will be quoted below the reply."
    font = "Couldn’t read your usual font, so this email may go out in your mail’s default font."
    old = [*guard, count, *stale, *shown, quoted, font, "Attachment: menu.pdf (12 KB)"]
    text = HEADER + "".join(f"Note: {line}\r\n" for line in old) + BODY
    path, draft_id = _draft(scope, "multi.md", text=text)
    await convert_open_drafts()
    assert path.read_bytes() == (HEADER + BODY).encode()
    row = scope["db"].execute("SELECT target_json FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
    assert json.loads(row[0])["notes"] == ["Existing", "\n".join([count, quoted, font])]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["sent", "discarded", "sending"])
async def test_non_open_draft_is_untouched(ownership_scope, state):
    from connections.mail.draft_format_conversion import convert_open_drafts
    scope = ownership_scope
    path, draft_id = _draft(scope, "settled.md", state=state)
    before = dict(scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())
    await convert_open_drafts()
    assert path.read_bytes() == OLD.encode()
    assert dict(scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()) == before


@pytest.mark.asyncio
async def test_parse_failure_leaves_file_and_record_untouched(ownership_scope, caplog):
    from connections.mail.draft_format_conversion import convert_open_drafts
    scope = ownership_scope
    broken = OLD.replace("To: sam@example.com", "To: not-an-address")
    path, draft_id = _draft(scope, "broken.md", text=broken)
    before = dict(scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())
    await convert_open_drafts()
    assert path.read_bytes() == broken.encode()
    assert dict(scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()) == before
    assert "could not be parsed" in caplog.text
    # It can never convert, so it doesn't make every later start read the drafts again.
    from infra.settings import get_setting
    assert get_setting("email_draft_format") == {"converted": True}


@pytest.mark.asyncio
async def test_a_draft_whose_file_is_gone_does_not_hold_the_pass_open(ownership_scope, caplog):
    from connections.mail.draft_format_conversion import convert_open_drafts
    from infra.settings import get_setting
    scope = ownership_scope
    path, _ = _draft(scope, "gone.md")
    path.unlink()
    await convert_open_drafts()
    assert "has no file" in caplog.text
    assert get_setting("email_draft_format") == {"converted": True}


@pytest.mark.asyncio
async def test_a_clean_pass_is_recorded_and_later_starts_read_no_drafts(ownership_scope, monkeypatch):
    from connections.mail import draft_format_conversion as conversion
    from infra.settings import get_setting
    scope = ownership_scope
    _draft(scope, "done.md")
    await conversion.convert_open_drafts()
    assert get_setting("email_draft_format") == {"converted": True}
    def unexpected():
        raise AssertionError("a start after a clean pass read the open drafts")
    monkeypatch.setattr(conversion, "_open_drafts", unexpected)
    await conversion.convert_open_drafts()


@pytest.mark.asyncio
async def test_only_document_line_breaks_separate_legacy_header_lines(ownership_scope):
    from connections.mail.draft_format_conversion import convert_open_drafts
    scope = ownership_scope
    text = OLD.replace("Subject: Lunch", "Subject: Lunch\u2028Note: still the subject")
    path, _ = _draft(scope, "line-breaks.md", text=text)
    await convert_open_drafts()
    assert path.read_bytes() == (HEADER + BODY).replace("Subject: Lunch", "Subject: Lunch\u2028Note: still the subject").encode()


@pytest.mark.asyncio
async def test_writer_failure_keeps_notes_for_the_next_start(ownership_scope, monkeypatch):
    from connections.mail import draft_format_conversion as conversion
    scope = ownership_scope
    path, draft_id = _draft(scope, "retry.md")
    async def refused(*args, **kwargs):
        raise OSError("test writer failure")
    with monkeypatch.context() as patch:
        patch.setattr(conversion, "save_document_text", refused)
        await conversion.convert_open_drafts()
    assert path.read_bytes() == OLD.encode()
    notes = json.loads(scope["db"].execute("SELECT target_json FROM action_drafts WHERE id=?", (draft_id,)).fetchone()[0])["notes"]
    assert notes == ["Existing", COUNT]
    from infra.settings import get_setting
    assert get_setting("email_draft_format") is None
    await conversion.convert_open_drafts()
    row = scope["db"].execute("SELECT target_json FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
    assert json.loads(row[0])["notes"] == notes
    assert path.read_bytes() == (HEADER + BODY).encode()
