"""The fresh read before an update reaches the person: the update's text and the documents it
hands them, read together; their passes move what the registered ones show the person.

The documents are exactly the openable document cards chat shows under the update
(``core.update_documents``), so what the person can open from the update is what was read,
and a document outside the writer's scope is neither a card nor sent to the reader.

A retry with an idempotency key that already delivered replays that delivery and needs no
read, since it delivers nothing new.
"""

from __future__ import annotations

from core import fresh_read
from core.fresh_read import Document, Item, Verdict
from infra.database import get_db
from infra.db_async import run_in_db


def _prepare(thread_id: str, text: str, idempotency_key: str | None) -> tuple[bool, str | None, tuple[Document, ...]]:
    """Blocking. Whether this key already delivered an update, the chat's person, and the documents it hands over."""
    from core.update_documents import linked_documents
    from registry.path_cards import targets
    from tools.post_update import _KEYED_SQL, _newest

    db = get_db()
    try:
        owner = db.execute("SELECT user_id, project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        if idempotency_key and _newest(db, _KEYED_SQL, thread_id, idempotency_key, None) is not None:
            return True, None, ()
        user_id = str(owner["user_id"]) if owner else None
        if owner is None or not targets(text):
            return False, user_id, ()
        documents: list[Document] = []
        for artifact_id, path in linked_documents(db, text, owner["user_id"], owner["project_id"]):
            try:
                documents.append(Document(artifact_id=artifact_id, name=path.name, data=path.read_bytes()))
            except OSError:
                continue
        return False, user_id, tuple(documents)
    finally:
        db.close()


async def read_update(thread_id: str, text: str, node_id: str | None, idempotency_key: str | None,
                      note_to_reader: str | None) -> Verdict:
    """Pass, fail or hold one update before anything is written; a replay passes with no read."""
    replay, user_id, documents = await run_in_db(_prepare, thread_id, text, idempotency_key)
    if replay:
        return Verdict(True, None)
    return await fresh_read.check(Item(kind="update", item_ref=thread_id, shown=text, writer_thread_id=thread_id,
                            user_id=user_id, node_id=node_id, documents=documents, note_to_reader=note_to_reader))
