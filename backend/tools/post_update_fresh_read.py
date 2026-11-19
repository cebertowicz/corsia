"""The fresh read before an update reaches the person: the update's text and the registered
documents it links, read together; their passes move what those documents show the person.

A retry with an idempotency key that already delivered replays that delivery and needs no
read, since it delivers nothing new.
"""

from __future__ import annotations

import re
from pathlib import Path

from core import fresh_read
from core.fresh_read import Document, Item, Verdict
from infra.database import get_db
from infra.db_async import run_in_db

#: Markdown link targets, in the ``[title](<path>)`` form agents are told to use and the bare ``(path)`` form.
_LINK = re.compile(r"\]\(\s*<([^>]+)>\s*\)|\]\(\s*([^)\s]+)\s*\)")


def _linked_documents(text: str) -> tuple[Document, ...]:
    """Blocking. Each registered, active document the text links to, with its current bytes."""
    from infra import config
    from registry import service as registry_service
    from tools.artifact_tool_helpers import tool_path

    found: dict[str, Document] = {}
    db = get_db()
    try:
        for match in _LINK.finditer(text):
            target = (match.group(1) or match.group(2) or "").strip()
            if not target or "://" in target or target.startswith(("#", "mailto:")):
                continue
            path = Path(tool_path(target, Path(config.WORKSPACE)))
            art = registry_service.find_file_identity(str(path), db=db)
            if art is None or art["state"] != "active" or art["type"] != "document" or art["id"] in found:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            found[art["id"]] = Document(artifact_id=str(art["id"]), name=path.name, data=data)
    finally:
        db.close()
    return tuple(found.values())


def _already_delivered(thread_id: str, idempotency_key: str | None) -> tuple[bool, str | None]:
    """Blocking. Whether this key already delivered an update, and the chat's person."""
    from tools.post_update import _KEYED_SQL, _newest

    db = get_db()
    try:
        owner = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        prior = _newest(db, _KEYED_SQL, thread_id, idempotency_key, None) if idempotency_key else None
        return prior is not None, (str(owner["user_id"]) if owner else None)
    finally:
        db.close()


async def read_update(thread_id: str, text: str, node_id: str | None, idempotency_key: str | None,
                      note_to_reader: str | None) -> Verdict:
    """Pass, fail or hold one update before anything is written; a replay passes with no read."""
    replay, user_id = await run_in_db(_already_delivered, thread_id, idempotency_key)
    if replay:
        return Verdict(True, None)
    documents = await run_in_db(_linked_documents, text)
    return await fresh_read.check(Item(kind="update", item_ref=thread_id, shown=text, writer_thread_id=thread_id,
                            user_id=user_id, node_id=node_id, documents=documents, note_to_reader=note_to_reader))
