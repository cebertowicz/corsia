"""FTS5 virtual table creation, sync, and query escaping."""

from __future__ import annotations

import sqlite3

_QUOTE = '"'


def fts5_escape(query: str, *, prefix: bool = False) -> str:
    """Escape a user query for FTS5 MATCH syntax.

    Each word becomes a quoted term, which is what makes punctuated input
    like ``eu-west-1`` match as a phrase instead of parsing as FTS5 operators.

    An embedded quote is doubled, because FTS5 reads a bare one as the end of
    the string: without this, searching ``alpha"`` builds an unterminated
    expression and raises ``sqlite3.OperationalError`` instead of returning
    results.

    ``prefix`` appends the FTS5 prefix operator to each term, so ``poll``
    matches ``polling``. It is off by default because raw-message search has
    always matched whole tokens and widening that silently would change what
    every history search returns. Callers that replaced a substring scan opt
    in, to keep the recall their users already had.

    This lives beside the index definitions rather than in the tool layer so
    that both ``tools`` and ``memory`` can use one copy; the dependency runs
    tools -> memory -> infra, and a second copy in either would be a second
    place for this bug to live.
    """
    words = query.strip().split()
    if not words:
        return _QUOTE * 2
    star = "*" if prefix else ""
    return " ".join(
        _QUOTE + word.replace(_QUOTE, _QUOTE * 2) + _QUOTE + star for word in words
    )


def _init_messages_fts(conn: sqlite3.Connection) -> None:
    """Create the message index without copying message content."""
    conn.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, role, content=messages, content_rowid=rowid
        )
    """)
    conn.execute("""
        CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, role)
                VALUES (new.rowid, new.content, new.role);
        END
    """)
    conn.execute("""
        CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, role)
                VALUES ('delete', old.rowid, old.content, old.role);
        END
    """)
    conn.execute("""
        CREATE TRIGGER messages_fts_update
        AFTER UPDATE OF content, role ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, role)
                VALUES ('delete', old.rowid, old.content, old.role);
            INSERT INTO messages_fts(rowid, content, role)
                VALUES (new.rowid, new.content, new.role);
        END
    """)


def comment_search_words_sql(user_message_id: str, cid: str) -> str:
    """SQL for the words one chat comment is searched by, or NULL when its input has none.

    A comment lives in its input's ``metadata.reply_context.comments``; ``chat_comments`` holds
    only its keys. The words are the input's own text, then the comment's quote and note, so a
    search can match a word from the text and one from the comment together. The two arguments
    are SQL expressions for the comment's keys (``new.user_message_id``, ``cc.cid``).

    This is the one definition: the insert trigger and ``infra.comment_search_fill`` both use it.
    Malformed metadata or a non-object comment yields no row rather than an error, because the
    trigger runs inside the transaction that stores the person's input.
    """
    return (
        f"(SELECT coalesce(m.content, '') || char(10) || {_comment_words('j')} "
        f"FROM messages m, {comments_of_sql('m')} j "
        f"WHERE m.id = {user_message_id} AND {comment_field_sql('j', 'cid')} = {cid} LIMIT 1)"
    )


def comment_words_like_sql(message: str) -> str:
    """SQL that is true when one of ``message``'s chat comments has a quote or note LIKE ``?``.

    The search fallback's counterpart of ``comment_search_words_sql``: the same comments and the
    same quote and note fields, so a word that appears only in another metadata key or line
    (``in_view``, ``first_unread``, attachment fields, the key names themselves) never matches.
    ``message`` is the SQL alias of the ``messages`` row; it takes one parameter, the pattern.
    """
    return f"EXISTS (SELECT 1 FROM {comments_of_sql(message)} j WHERE {_comment_words('j')} LIKE ?)"


def comments_of_sql(message: str) -> str:
    """The comments of a ``messages`` row; malformed metadata yields none rather than an error."""
    return (f"json_each(CASE WHEN json_valid({message}.metadata) THEN {message}.metadata ELSE '{{}}' END, "
            "'$.reply_context.comments')")


def comment_field_sql(comment: str, name: str) -> str:
    """One field of a comment from ``comments_of_sql``; NULL for a comment that is not an object."""
    return f"CASE WHEN {comment}.type = 'object' THEN json_extract({comment}.value, '$.{name}') END"


def _comment_words(comment: str) -> str:
    """The searchable words of one comment: its quote, then its note."""
    return (f"coalesce({comment_field_sql(comment, 'quote')}, '') || char(10) "
            f"|| coalesce({comment_field_sql(comment, 'note')}, '')")


def _init_comment_fts(conn: sqlite3.Connection) -> None:
    """Index chat-comment words for history search: one row per ``chat_comments`` row, by its rowid.

    Contentless, so no words are stored twice, and ``contentless_delete`` so a row is removed by
    rowid alone. ``messages_fts`` is left as it is: a search reads both, message hits first.
    Comments stored before this table existed are filled after startup by
    ``infra.comment_search_fill``, never here.
    """
    conn.execute("""
        CREATE VIRTUAL TABLE comment_fts USING fts5(
            words, content='', contentless_delete=1
        )
    """)
    words = comment_search_words_sql("new.user_message_id", "new.cid")
    conn.execute(f"""
        CREATE TRIGGER comment_fts_insert AFTER INSERT ON chat_comments BEGIN
            INSERT OR REPLACE INTO comment_fts(rowid, words)
                SELECT new.rowid, w FROM (SELECT {words} AS w) WHERE w IS NOT NULL;
        END
    """)
    conn.execute("""
        CREATE TRIGGER comment_fts_delete AFTER DELETE ON chat_comments BEGIN
            DELETE FROM comment_fts WHERE rowid = old.rowid;
        END
    """)


def _init_checkpoints_fts(conn: sqlite3.Connection) -> None:
    """Index checkpoint narratives for bounded search.

    External-content like the message index, so narratives are not stored
    twice. ``generation_order`` is the table's INTEGER PRIMARY KEY and
    therefore its rowid, which is what lets the index stay in sync by rowid.

    Only the narrative text column is indexed. A checkpoint's row is written
    empty and filled at capture, so the update trigger carries the text in
    when it arrives rather than leaving the index behind.
    """
    conn.execute("""
        CREATE VIRTUAL TABLE checkpoints_fts USING fts5(
            raw_result,
            content=checkpoints, content_rowid=generation_order
        )
    """)
    conn.execute("""
        CREATE TRIGGER checkpoints_fts_insert AFTER INSERT ON checkpoints BEGIN
            INSERT INTO checkpoints_fts(rowid, raw_result)
                VALUES (new.generation_order, new.raw_result);
        END
    """)
    conn.execute("""
        CREATE TRIGGER checkpoints_fts_delete AFTER DELETE ON checkpoints BEGIN
            INSERT INTO checkpoints_fts(checkpoints_fts, rowid, raw_result)
                VALUES ('delete', old.generation_order, old.raw_result);
        END
    """)
    conn.execute("""
        CREATE TRIGGER checkpoints_fts_update
        AFTER UPDATE OF raw_result ON checkpoints BEGIN
            INSERT INTO checkpoints_fts(checkpoints_fts, rowid, raw_result)
                VALUES ('delete', old.generation_order, old.raw_result);
            INSERT INTO checkpoints_fts(rowid, raw_result)
                VALUES (new.generation_order, new.raw_result);
        END
    """)


def _init_checkpoint_entry_search_fts(conn: sqlite3.Connection) -> None:
    """Create the entry-level FTS index over ``checkpoint_entry_search``.

    Rows are written only by ``infra.checkpoint_entry_index``, which alone
    decides validity through the shared snapshot reader; this index merely
    mirrors that table's text.
    """
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS checkpoint_entry_search_fts USING fts5(
            search_text,
            content=checkpoint_entry_search,
            content_rowid=rowid
        )
    """)
    _init_checkpoint_entry_search_fts_triggers(conn)


def _init_checkpoint_entry_search_fts_triggers(conn: sqlite3.Connection) -> None:
    """Keep the external-content index in sync with its entry rows."""
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS checkpoint_entry_search_fts_insert
        AFTER INSERT ON checkpoint_entry_search BEGIN
            INSERT INTO checkpoint_entry_search_fts(rowid, search_text)
                VALUES (new.rowid, new.search_text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS checkpoint_entry_search_fts_delete
        AFTER DELETE ON checkpoint_entry_search BEGIN
            INSERT INTO checkpoint_entry_search_fts(
                checkpoint_entry_search_fts, rowid, search_text)
                VALUES ('delete', old.rowid, old.search_text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS checkpoint_entry_search_fts_update
        AFTER UPDATE OF search_text ON checkpoint_entry_search BEGIN
            INSERT INTO checkpoint_entry_search_fts(
                checkpoint_entry_search_fts, rowid, search_text)
                VALUES ('delete', old.rowid, old.search_text);
            INSERT INTO checkpoint_entry_search_fts(rowid, search_text)
                VALUES (new.rowid, new.search_text);
        END
    """)


def _init_fts(conn: sqlite3.Connection) -> None:
    """Create the exact fresh-schema FTS indexes."""
    _init_messages_fts(conn)
    _init_comment_fts(conn)
    _init_checkpoints_fts(conn)
    _init_checkpoint_entry_search_fts(conn)
