"""Startup conversion of open email draft headers to the single document format.

Only header lines move; body bytes stay as written. Notes are stored before the
validated writer removes them, so an interrupted conversion cannot lose them.
A converted file has no legacy lines and is never rewritten on later starts.
"""
from __future__ import annotations

import json
import logging

from connections.mail.email_document import parse_document
from doceditor.document_save import DocumentSave, save_document_text
from doceditor.document_scope import revision
from infra import config
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_off_db
from tools.action_drafts import record_draft
from tools.reviewed_drafts import draft_target

log = logging.getLogger(__name__)


def _open_drafts() -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute(
            "SELECT d.*,t.user_id,t.project_id FROM action_drafts d JOIN threads t ON t.id=d.thread_id "
            "WHERE d.kind='email' AND d.state='open'")]
    finally:
        db.close()


def _converted(text: str) -> tuple[str, list[str]]:
    """Remove the two old header-only fields, preserving line endings and the entire body."""
    kept, notes = [], []
    in_header = True
    for line in text.split("\n"):
        raw = line.removesuffix("\r")
        if not raw:
            in_header = False
        key, colon, value = raw.partition(":")
        if in_header and colon and key.strip().lower() in {"note", "in reply to"}:
            if key.strip().lower() == "note":
                notes.append(value.removeprefix(" "))
        else:
            kept.append(line)
    return "\n".join(kept), notes


def _store_notes(row: dict, notes: list[str]) -> dict:
    target = draft_target(row)
    target["notes"] = list(dict.fromkeys([*(target.get("notes") or []), *notes]))
    with write_transaction(None, scope="email_draft_format_notes") as db:
        db.execute("UPDATE action_drafts SET target_json=? WHERE id=? AND state='open'",
                   (json.dumps(target), row["id"]))
    return target


async def convert_open_drafts() -> None:
    """Run before live services start; invalid or unavailable files stay untouched and are logged."""
    root = config.WORKSPACE.resolve()
    for row in await run_in_db(_open_drafts):
        try:
            path = (root / row["path"]).resolve()
            if not path.is_relative_to(root):
                raise ValueError("draft path is outside the workspace")
            before = await run_off_db(path.read_bytes)
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
