"""Open email drafts convert through the document writer without changing their body or losing notes."""
import json
import os

import pytest

from connections.mail.email_document import parse_document
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write
from tools.action_drafts import create_draft

HEADER = "From: me@example.com\r\nTo: sam@example.com\r\nSubject: Lunch\r\n"
BODY = "\r\nDo not alter this.\r\nNote: this is body text\r\nIn reply to: also body text\r\n\r\n-- \r\nSam\r\n"
OLD = HEADER + "In reply to: m-1\r\nNote: same subject in another conversation\r\nNote:  indented note  \r\n" + BODY


def _draft(scope, name, *, state="open", text=OLD):
    _write(scope, name, text)
    path = scope["category"] / name
    relative = os.path.relpath(path, scope["workspace"])
    saved = create_draft(scope["owner"], "email", relative, path.read_bytes(), {"account_id": "a", "notes": ["Existing"]})
    scope["db"].execute("UPDATE action_drafts SET state=? WHERE id=?", (state, saved["draft_id"]))
    scope["db"].commit()
    return path, saved["draft_id"]


@pytest.mark.asyncio
async def test_open_draft_converts_once_preserving_body_and_old_guard_notes(ownership_scope):
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
    assert json.loads(row["target_json"])["notes"] == ["Existing", "same subject in another conversation", " indented note  "]
    first = dict(row)
    await convert_open_drafts()
    assert path.read_bytes() == after
    assert dict(scope["db"].execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()) == first


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
    assert "same subject in another conversation" in notes
    await conversion.convert_open_drafts()
    row = scope["db"].execute("SELECT target_json FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
    assert json.loads(row[0])["notes"] == notes
    assert path.read_bytes() == (HEADER + BODY).encode()
