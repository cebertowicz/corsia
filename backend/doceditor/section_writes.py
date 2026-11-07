"""The section writer turns every agent edit or suggestion on an open document passes through.

An agent's ``edit_file``, ``multi_edit`` or ``write_file`` on a live document (``live_tools``) and
its suggestion (``agent_comment_writes.reply``) touch one or more sections (``doc_sections``). The
call runs holding every one of those sections' turns. When a person is editing one of them, or
another agent's write holds it or waits for it first, the call is saved in that section's line
(``writer_line``) and not run, and the caller returns the line's answer. The wake (``section_wake``)
shows the saved call; making the same call again posts it, through this same gate.

A call let through answers this agent's saved rows in the sections it touches, and only those: a
woken row in a section the call does not touch stays woken. It settles them in the admitting
transaction: the edit itself lands in the document's live store, which no database transaction
holds, and a call refused after that reports the refusal in its own result. A call that touches no
section takes no turn and settles nothing. A document no one has open is not gated: no person can
hold its sections. A woken agent may still answer there (``settle_closed``).

Cost per call touching ``k`` sections: when nothing is waiting or woken on them and the agent has
no woken section row, the turns are taken in memory after one read, with no write transaction;
leaving is one write transaction with ``k`` line reads.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from loguru import logger

from doceditor import doc_sections, writer_line, writer_turns
from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import QuoteResolveError, _cp_at_u16, resolve_quote
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db

#: Most of a section's text saved with a write, for the wake's diff.
BASE_LIMIT = 20_000


def edit_sections(text: str, edits: list[dict[str, Any]]) -> list[str]:
    """The sections an edit's exact ``old_string`` matches fall in; a fuzzy match is not found here.

    The text is scanned for headings once; each match is placed in it by bisect.
    """
    sections = doc_sections.Sections(text)
    touched: dict[str, None] = {}
    for edit in edits:
        old = edit["old_string"]
        start = text.find(old) if old else -1
        while start >= 0:
            touched.update(dict.fromkeys(sections.between(start, start + len(old))))
            start = text.find(old, start + 1) if edit.get("replace_all") else -1
    return list(touched)


def write_sections(text: str, content: str) -> list[str]:
    """The sections of ``text`` a whole-document write changes."""
    if text == content:
        return []
    start, end, _ = doc_sections.changed_span(text, content)
    return doc_sections.sections_in(text, start, end)


def call_sections(text: str, call: dict[str, Any]) -> list[str]:
    """The sections of ``text`` a saved write call (``write_live``'s) touches."""
    return write_sections(text, call["content"]) if "content" in call else edit_sections(text, call["edits"])


def quote_sections(text: str, quote: str | None, near: str | None) -> list[str]:
    """The sections a suggestion's quote spans; none when it does not resolve (creation reports that)."""
    if not quote:
        return []
    try:
        from16, to16 = resolve_quote(text, quote, near)
    except (QuoteResolveError, ValueError):
        return []
    start, end = _cp_at_u16(text, from16), _cp_at_u16(text, to16)
    return [] if start is None or end is None else doc_sections.sections_in(text, start, end)


def saved_op(kind: str, call: dict[str, Any], text: str, sections: list[str], **extra: Any) -> dict[str, Any]:
    """What a saved section write keeps: the call to repeat and each touched section as it was."""
    base: dict[str, str] = {}
    if sections:
        scanned = doc_sections.Sections(text)
        base = {section: (scanned.text_of(section) or "")[:BASE_LIMIT] for section in sections}
    return {"kind": kind, "call": call, "base": base, **extra}


def _user(db, thread_id: str) -> str:
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return str(row["user_id"]) if row is not None and row["user_id"] else ""


def _admit_all(db, *, root_ids: list[str], artifact_id: str, thread_id: str, user_id: str,
               op: dict[str, Any]) -> list[writer_line.Admitted] | writer_line.Queued:
    """``writer_line.admit`` for one write that touches several turns: it takes them all, or waits on the first taken."""
    def taken(root_id: str) -> bool:
        line = writer_line.active(db, root_id)
        return (writer_turns.person_holding(root_id) is not None
                or writer_turns.agent_writing(root_id) not in (None, thread_id)
                or bool(line) and line[0]["agent_thread_id"] != thread_id)

    admitted: list[writer_line.Admitted] = []
    for root_id in sorted(root_ids, key=lambda key: not taken(key)):
        admission = writer_line.admit(db, root_id=root_id, artifact_id=artifact_id, thread_id=thread_id,
                                      user_id=user_id, op=op)
        if isinstance(admission, writer_line.Queued):
            for held in admitted:
                writer_turns.agent_exit(held.root_id, thread_id)
            return admission
        admitted.append(admission)
    return admitted


def _alone(db, keys: list[str], thread_id: str) -> bool:
    """Take every key's turn in memory when nothing waits or is woken on them and this agent has no woken row.

    One read and no write transaction. A line row saved between that read and the claim is served
    by this call's ``_finish``, which holds the write lock; one woken in that instant is posted
    after this write, which the agent is told.
    """
    if writer_turns.may_answer_section(thread_id):
        return False
    waiting = db.execute(
        f"SELECT 1 FROM doc_comment_writer_line WHERE root_id IN ({','.join('?' * len(keys))}) LIMIT 1", keys,
    ).fetchone()
    return waiting is None and writer_turns.agents_claim(keys, thread_id)


def _admit(artifact_id: str, keys: list[str], thread_id: str, op: dict[str, Any]):
    """Take every touched section's turn, or save ``op`` in line; let through, it answers the agent's rows.

    Returns ``Queued``, or the keys to release and serve once the call ends.
    """
    db = get_db()
    try:
        if _alone(db, keys, thread_id):
            return keys
        with write_transaction(db, scope="section_line_admit") as connection:
            admission = _admit_all(connection, root_ids=keys, artifact_id=artifact_id, thread_id=thread_id,
                                   user_id=_user(connection, thread_id), op=op)
            if isinstance(admission, writer_line.Queued):
                return admission
            _answered(connection, thread_id, op["call"], keys)
            return keys
    except BaseException:
        for key in keys:
            writer_turns.agent_exit(key, thread_id)
        raise
    finally:
        db.close()


def _answered(db, thread_id: str, call: dict[str, Any], keys: Sequence[str]) -> list[str]:
    """Settle this agent's rows in the sections ``keys``: posted as is when ``call`` is a saved one,
    else rewritten. Returns the keys of the rows settled."""
    rows = db.execute(
        f"""SELECT * FROM doc_comment_writer_line
           WHERE agent_thread_id=? AND root_id IN ({','.join('?' * len(keys))})""",
        (thread_id, *keys),
    ).fetchall()
    for row in rows:
        as_is = any(saved.get("call") == call for saved in json.loads(row["ops"]))
        writer_line.settle(db, row, "posted_as_is" if as_is else "rewritten")
    return [str(row["root_id"]) for row in rows]


def _finish(keys: list[str], thread_id: str) -> list[str]:
    """The call ended: release its turns and serve each line it held or answered."""
    db = get_db()
    try:
        with write_transaction(db, scope="section_line_finish") as connection:
            drives: list[str] = []
            for key in keys:
                writer_turns.agent_exit(key, thread_id)
                drives += writer_line.serve(connection, key)
            return drives
    finally:
        db.close()


async def through(artifact_id: str, sections: list[str], thread_id: str, op: dict[str, Any],
                  run: Callable[[], Awaitable[Any]]):
    """Run ``run`` holding every touched section's turn, or save ``op`` in line and return ``Queued``.

    A call touching no section has no turn to take. A failure passing the turns on strands no one:
    they are released in memory, and the continuation sweep serves any line still waiting.
    """
    from doceditor.agent_comment_writes import _drive

    if not sections:
        return await run()
    keys = [writer_turns.section_key(artifact_id, section) for section in sections]
    admission = await run_in_db(_admit, artifact_id, keys, thread_id, op)
    if isinstance(admission, writer_line.Queued):
        return admission
    try:
        return await run()
    finally:
        try:
            await _drive(await run_in_db(_finish, admission, thread_id))
        except Exception:
            for key in keys:
                writer_turns.agent_exit(key, thread_id)
            logger.exception("section turns on {} were not passed on after {}'s write", artifact_id, thread_id)


def _settle_closed(path: str | None, artifact_id: str | None, thread_id: str, call: dict[str, Any]) -> list[str]:
    from registry.models import get_artifact
    from registry.service import resolve_file_identity

    db = get_db()
    try:
        artifact = resolve_file_identity(path, db=db) if path else get_artifact(artifact_id or "", db=db)
        if artifact is None:
            return []
        try:
            text = read_doc_text(path or str(artifact["path"]))
            keys = (quote_sections(text, call.get("quote"), call.get("near")) if "replacement" in call
                    else call_sections(text, call))
        except (OSError, ValueError):
            return []
        if not keys:
            return []
        with write_transaction(db, scope="section_line_closed") as connection:
            return [thread for key in _answered(connection, thread_id, call,
                                                [writer_turns.section_key(str(artifact["id"]), key) for key in keys])
                    for thread in writer_line.serve(connection, key)]
    finally:
        db.close()


async def settle_closed(path: str | None, thread_id: str, call: dict[str, Any], artifact_id: str | None = None) -> None:
    """A woken agent is writing to the document after everyone closed it: that answers its wake.
    The document is found by ``path``, or by ``artifact_id`` for a suggestion.

    Settled as the write starts, since the file write that follows has no hook of its own; a write
    that then fails tells the agent so in its own result.
    """
    from doceditor.agent_comment_writes import _drive

    if not writer_turns.may_answer_section(thread_id):
        return
    try:
        await _drive(await run_in_db(_settle_closed, path, artifact_id, thread_id, call))
    except Exception:
        logger.exception("section rows of {} on {} did not settle", thread_id, path or artifact_id)
