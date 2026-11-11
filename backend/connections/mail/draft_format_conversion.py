"""Startup conversion of open email draft headers to the single document format.

Only header lines move; body bytes stay as written. Notes are stored before the
validated writer removes them, so an interrupted conversion cannot lose them.
A converted file has no legacy lines and is never rewritten on later starts.
A draft that can never convert (its file gone, outside the workspace, or not a draft
document) is logged once and left as it is. Once a start passes every open draft without
any other failure, the ``email_draft_format`` setting records it and later starts skip
the pass; until then each start retries.
"""
from __future__ import annotations

import json
import logging
import re

from connections.mail.email_document import parse_document
from doceditor.document_save import DocumentSave, save_document_text
from doceditor.document_scope import revision
from infra import config
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_off_db
from infra.settings import get_setting, set_setting
from tools.action_drafts import record_draft
from tools.reviewed_drafts import draft_target

log = logging.getLogger(__name__)

DONE_SETTING = "email_draft_format"


def _open_drafts() -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute(
            "SELECT d.*,t.user_id,t.project_id FROM action_drafts d JOIN threads t ON t.id=d.thread_id "
            "WHERE d.kind='email' AND d.state='open'")]
    finally:
        db.close()


# The old writer put every note on its own ``Note:`` lines, one per line of the note, so where one note ends is lost.
# Only the one-line notes the person still gets are kept: the sender's email count, a reply sent to another address,
# the quoted-email line and the font line. Everything else is the guard's evidence, which now goes only to the agent,
# or what the view shows elsewhere (attachment chips, the conversation). Text inside an email's untrusted-email
# wrapper is never a note's start, since an email can contain any line.
_KEPT = re.compile(
    r"\d+ emails from .* in the last 30 days\.$"
    r"|The requested recipient differs from the original reply address "
    r"|The earlier message from .* will be quoted below the reply\.$"
    r"|Couldn’t read your usual font, ")
_OPEN, _CLOSE = "<untrusted-email", "</untrusted-email>"


def _converted(text: str) -> tuple[str, list[str]]:
    """Remove the two old header-only fields, preserving line endings and the entire body.

    The old note lines become one note, since their boundaries can't be recovered; the next save of the draft
    replaces it with notes made the current way.
    """
    kept, lines = [], []
    in_header, in_email = True, False
    for line in text.split("\n"):
        raw = line.removesuffix("\r")
        if not raw:
            in_header = False
        key, colon, value = raw.partition(":")
        if in_header and colon and key.strip().lower() in {"note", "in reply to"}:
            if key.strip().lower() == "note":
                note = value.removeprefix(" ")
                if not in_email and _KEPT.match(note):
                    lines.append(note)
                if _OPEN in note:
                    in_email = True
                if _CLOSE in note.rpartition(_OPEN)[2]:
                    in_email = False
        else:
            kept.append(line)
    return "\n".join(kept), ["\n".join(lines)] if lines else []


def _store_notes(row: dict, notes: list[str]) -> dict:
    """Append the converted note.

    A start interrupted after this write converts the same file again; its note is already the tail, so it isn't
    added twice.
    """
    target = draft_target(row)
    stored = list(target.get("notes") or [])
    if notes and stored[-len(notes):] != notes:
        stored.extend(notes)
    target["notes"] = stored
    with write_transaction(None, scope="email_draft_format_notes") as db:
        db.execute("UPDATE action_drafts SET target_json=? WHERE id=? AND state='open'",
                   (json.dumps(target), row["id"]))
    return target


async def convert_open_drafts() -> None:
    """Run before live services start; invalid or unavailable files stay untouched and are logged."""
    if (await run_in_db(get_setting, DONE_SETTING) or {}).get("converted") is True:
        return
    root = config.WORKSPACE.resolve()
    failed = False
    for row in await run_in_db(_open_drafts):
        try:
            # These never clear on a later start, so they are logged and left; they don't hold the pass open.
            path = (root / row["path"]).resolve()
            if not path.is_relative_to(root):
                log.warning("Open email draft %s is outside the workspace; left as it is", row["id"])
                continue
            try:
                before = await run_off_db(path.read_bytes)
            except FileNotFoundError:
                log.warning("Open email draft %s has no file; nothing to convert", row["id"])
                continue
            text, notes = _converted(before.decode("utf-8"))
            if text.encode("utf-8") == before:
                continue
            if parse_document(text) is None:
                log.warning("Open email draft %s could not be parsed; file left untouched", row["id"])
                continue
            target = await run_in_db(_store_notes, row, notes)
            await save_document_text(
                DocumentSave(path=str(path), text=text, thread_id=row["thread_id"], project_id=row["project_id"]),
                row["user_id"], by_agent=True, reason="Convert email draft header", expected_sha256=revision(before))
            await run_in_db(record_draft, row["id"], text.encode("utf-8"), target)
        except Exception:
            log.exception("Open email draft %s could not be converted", row["id"])
            failed = True
    if not failed:
        await run_in_db(set_setting, DONE_SETTING, {"converted": True})
