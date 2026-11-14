"""Schema 194 -> 195: drop a closing-text answer that repeats an answer a sent message already gave.

From 195 on, ``chat_comment_answers.place_answers`` places an answer only from words the person is
shown. Before, an agent that answered a comment with ``post_update`` and again in its closing text
had both stored, and the comment thread showed the answer twice. This step deletes the stored row
of a closing-text answer (no ``for_user``) only when a ``for_user`` message in the same Turn gave
the same comment an answer with the same words, compared after whitespace normalisation. A closing
answer that says something different keeps its row, so a reply the person may already have read is
never removed from their thread.

It deletes rows from ``chat_comment_answers`` only; no schema changes. It logs one INFO line with
the rows it deleted (exact repeats), the rows it kept because the words differ, the table total and
the elapsed time, which is how a production run is read. Runs inside the runner's transaction; it
never commits or rolls back.
"""

from __future__ import annotations

import sqlite3
import time

from loguru import logger

from infra.chat_comment_answers import parse_answers

_CANDIDATES = (
    "SELECT a.rowid, a.cid, closing.id, closing.content, sent.id, sent.content "
    "FROM chat_comment_answers a "
    "JOIN messages closing ON closing.id=a.assistant_message_id "
    "JOIN chat_comment_answers o ON o.thread_id=a.thread_id AND o.user_message_id=a.user_message_id "
    "AND o.cid=a.cid AND o.assistant_message_id<>a.assistant_message_id "
    "JOIN messages sent ON sent.id=o.assistant_message_id "
    "WHERE sent.role='assistant' AND json_valid(sent.metadata) AND json_extract(sent.metadata,'$.for_user')=1 "
    "AND COALESCE(CASE WHEN json_valid(closing.metadata) THEN json_extract(closing.metadata,'$.for_user') END,0)=0 "
    "AND json_extract(closing.metadata,'$.turn_id') IS NOT NULL "
    "AND json_extract(closing.metadata,'$.turn_id')=json_extract(sent.metadata,'$.turn_id')"
)


def _words(cache: dict[str, dict[str, str]], message_id: str, content: str | None) -> dict[str, str]:
    """Each comment's answer in a message, its sections joined, whitespace normalised."""
    if message_id not in cache:
        parsed = parse_answers(content or "", message_id)
        cache[message_id] = {
            cid: " ".join(" ".join(section["text"] for section in sections).split())
            for cid, sections in parsed.items()
        }
    return cache[message_id]


def migrate_194_to_195(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"chat_comment_answers", "messages"} <= tables:
        return
    started = time.monotonic()
    total = conn.execute("SELECT count(*) FROM chat_comment_answers").fetchone()[0]
    cache: dict[str, dict[str, str]] = {}
    repeats: set[int] = set()
    different: set[int] = set()
    for rowid, cid, closing_id, closing, sent_id, sent in conn.execute(_CANDIDATES).fetchall():
        closing_words = _words(cache, closing_id, closing).get(cid)
        if closing_words is not None and closing_words == _words(cache, sent_id, sent).get(cid):
            repeats.add(rowid)
        else:
            different.add(rowid)
    # A row repeating any one sent answer is an exact repeat, however many sent answers there are.
    different -= repeats
    conn.executemany("DELETE FROM chat_comment_answers WHERE rowid=?", [(rowid,) for rowid in repeats])
    logger.info(
        "migration 194->195 closing-text answers: deleted={} kept_different={} total={} elapsed_ms={}",
        len(repeats), len(different), total, round((time.monotonic() - started) * 1000),
    )
