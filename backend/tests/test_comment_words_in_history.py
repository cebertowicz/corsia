"""A person's chat-comment words stay readable and findable in history.

A comment-only input stores no text: its quote and note live in
``metadata.reply_context.comments``. ``get_thread_history`` shows them as the agent got
them, and ``search_history`` finds them through ``comment_fts``, with message hits first.
Comments stored before the index existed are filled after startup by
``infra.comment_search_fill``.
"""

from __future__ import annotations

import json

import pytest

THREAD = "t-comments"
USER = "u"


def _db():
    from infra.database import get_db

    return get_db()


def _row(seq: int, role: str, content: str, metadata: dict | None = None) -> str:
    message_id = f"m{seq}"
    db = _db()
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,delivery_state,seq)"
        " VALUES (?,?,?,?,?,?,'delivered',?)",
        (message_id, THREAD, role, content, json.dumps(metadata) if metadata else None,
         f"2026-11-19T00:00:{seq:02d}Z", seq),
    )
    db.commit()
    return message_id


def _input(seq: int, content: str, comments: list[dict]) -> str:
    """Store a person's input and place its comments the way admission does."""
    from infra.chat_comment_index import place_comments

    metadata = {"reply_context": {"comments": comments}}
    message_id = _row(seq, "user", content, metadata)
    db = _db()
    place_comments(db, THREAD, message_id, metadata, "2026-11-19T00:00:00Z")
    db.commit()
    return message_id


def _comment(cid: str, quote: str, note: str, seq: int = 1) -> dict:
    return {"cid": cid, "seq": seq, "quote": quote, "note": note}


def _search(query: str, **kwargs) -> list[str]:
    from infra.thread_confinement import Reader
    from tools.query_helpers import search_messages_fts

    return [row["id"] for row in search_messages_fts(_db(), query, reader=Reader.person(), **kwargs)]


def _drop_comment_index() -> None:
    """The database as schema 201 left it: no comment index, no triggers."""
    db = _db()
    db.execute("DROP TRIGGER comment_fts_insert")
    db.execute("DROP TRIGGER comment_fts_delete")
    db.execute("DROP TABLE comment_fts")
    db.commit()


def _migrate() -> None:
    from infra.migration_203_to_204_comment_search import migrate_203_to_204

    db = _db()
    migrate_203_to_204(db)
    db.commit()


def _fill_all() -> None:
    from infra.comment_search_fill import MORE, fill_batch

    while fill_batch(_db()) == MORE:
        pass


@pytest.fixture
def chat(isolated_db, monkeypatch):  # noqa: ARG001 - isolated_db builds the schema
    """One thread with an agent message the comments point at, and the startup FTS indexes."""
    from infra.db_fts import _init_comment_fts, _init_messages_fts

    db = _db()
    db.execute(
        "INSERT INTO threads(id,user_id,model,title,created_at,updated_at) VALUES (?,?,'m','Comments','t','t')",
        (THREAD, USER),
    )
    _init_messages_fts(db)
    _init_comment_fts(db)
    db.commit()
    _row(1, "assistant", "The release plan is ready for review.")

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    for module in ("tools.query_projects", "tools.query_search"):
        monkeypatch.setattr(f"{module}.run_in_db", inline)
    return db


async def _history() -> str:
    from tools.query_projects import GetThreadHistoryTool

    return await GetThreadHistoryTool().execute(THREAD, _user_id=USER)


async def test_comment_only_input_shows_its_comment_in_thread_history(chat):
    _input(2, "", [_comment("c2.1", "release plan", "run in parallel with workers")])

    out = await _history()

    assert '"release plan" | note: run in parallel with workers' in out


async def test_mixed_input_shows_its_text_and_its_comment(chat):
    _input(2, "Also check the zebra crossing.", [_comment("c2.1", "plan is ready", "ask about pelicans")])

    out = await _history()

    assert "Also check the zebra crossing." in out
    assert '"plan is ready" | note: ask about pelicans' in out


def test_search_finds_a_comment_by_its_quote_and_by_its_note(chat):
    _input(2, "", [_comment("c2.1", "release plan", "run in parallel with workers")])

    assert _search("parallel workers") == ["m2"]
    # The quote is the agent's own words, so the agent message is found too, first.
    assert _search("release plan") == ["m1", "m2"]


def test_search_finds_a_mixed_input_by_its_text_and_by_its_comment(chat):
    _input(2, "Also check the zebra crossing.", [_comment("c2.1", "plan is ready", "ask about pelicans")])

    assert _search("zebra") == ["m2"]
    assert _search("pelicans") == ["m2"]
    # A word from the text and a word from the comment, in one query.
    assert _search("zebra pelicans") == ["m2"]


def test_search_result_shows_the_comment_it_matched(chat):
    from infra.thread_confinement import Reader
    from tools.query_helpers import format_history_results, search_messages_fts

    _input(2, "", [_comment("c2.1", "release plan", "run in parallel with workers")])

    rows = search_messages_fts(_db(), "parallel", reader=Reader.person())

    assert "note: run in parallel with workers" in format_history_results(rows, "parallel", None, None)


def test_message_hits_keep_their_order_and_come_before_comment_hits(chat):
    _row(2, "assistant", "alpha")
    _row(3, "assistant", "alpha alpha alpha beta")
    _row(4, "user", "alpha beta gamma delta epsilon zeta eta theta")
    _input(5, "", [_comment("c5.1", "release plan", "alpha alpha alpha alpha alpha")])
    plain = [
        f"m{row[0]}" for row in _db().execute(
            "SELECT m.seq FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
            "WHERE messages_fts MATCH 'alpha' ORDER BY bm25(messages_fts)"
        )
    ]

    found = _search("alpha")

    # The order a plain message search gave before comments were indexed, then the comment hit,
    # however strong its own score.
    assert found == [*plain, "m5"]
    assert set(plain) == {"m2", "m3", "m4"}


def test_an_input_whose_text_repeats_its_comment_is_listed_once(chat):
    _input(2, "run in parallel with workers", [_comment("c2.1", "release plan", "run in parallel with workers")])

    assert _search("parallel") == ["m2"]


def test_words_split_over_two_comments_of_one_input_are_not_found_together(chat):
    """Known limit: each comment is its own index row, with the input's text."""
    _input(2, "", [
        _comment("c2.1", "release plan", "pelicans"),
        _comment("c2.2", "ready for review", "zebras"),
    ])

    assert _search("pelicans") == ["m2"]
    assert _search("zebras") == ["m2"]
    assert _search("pelicans zebras") == []


def test_a_bad_comment_never_blocks_storing_the_input(chat):
    """The index trigger runs inside the input's transaction, so it must not raise."""
    from infra.chat_comment_index import place_comments

    db = _db()
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,delivery_state,seq)"
        " VALUES ('m2',?,'user','',?,'t','delivered',2)",
        (THREAD, json.dumps({"reply_context": {"comments": ["not an object", {"cid": "c2.1", "seq": 1}]}})),
    )
    place_comments(db, THREAD, "m2", {"reply_context": {"comments": [{"cid": "c2.1", "seq": 1}]}}, "t")
    db.execute("UPDATE messages SET metadata = 'not json' WHERE id = 'm2'")
    db.execute("INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at)"
               " VALUES (?,'m1','m2','c2.2','t')", (THREAD,))
    db.commit()

    assert db.execute("SELECT count(*) FROM chat_comments WHERE user_message_id = 'm2'").fetchone()[0] == 2


def test_a_deleted_comment_is_no_longer_found(chat):
    _input(2, "", [_comment("c2.1", "release plan", "pelicans")])
    db = _db()
    db.execute("DELETE FROM chat_comments WHERE user_message_id = 'm2'")
    db.commit()

    assert _search("pelicans") == []


# The fill: comments stored before schema 204 ------------------------------------------------


def test_a_comment_stored_before_the_migration_is_found_after_the_fill(chat):
    _drop_comment_index()
    _input(2, "", [_comment("c2.1", "release plan", "pelicans")])
    _migrate()

    assert _search("pelicans") == []  # migrated, not yet filled: message hits only
    _fill_all()

    assert _search("pelicans") == ["m2"]


def test_the_migration_creates_only_the_index_and_its_triggers(chat):
    _drop_comment_index()
    _input(2, "", [_comment("c2.1", "release plan", "pelicans")])
    before = {row[0] for row in _db().execute("SELECT name FROM sqlite_master")}

    _migrate()

    added = {row[0] for row in _db().execute("SELECT name FROM sqlite_master")} - before
    assert {"comment_fts", "comment_fts_insert", "comment_fts_delete"} <= added
    assert all(name.startswith("comment_fts") for name in added)
    assert _db().execute("SELECT count(*) FROM comment_fts WHERE comment_fts MATCH 'pelicans'").fetchone()[0] == 0


def test_the_fill_resumes_after_an_interrupt(chat, monkeypatch):
    import infra.comment_search_fill as fill

    _drop_comment_index()
    for seq in range(2, 7):
        _input(seq, "", [_comment(f"c{seq}.1", "release plan", f"word{seq}")])
    _migrate()
    monkeypatch.setattr(fill, "BATCH_ROWS", 2)

    assert fill.fill_batch(_db()) == fill.MORE
    first = fill.get_setting(fill.MARKER, db=_db())
    assert first["filled"] == 2

    # The next batch fails after writing: it rolls back whole, progress included.
    real = fill.set_setting

    def interrupted(*args, **kwargs):
        raise RuntimeError("stopped")

    monkeypatch.setattr(fill, "set_setting", interrupted)
    with pytest.raises(RuntimeError):
        fill.fill_batch(_db())
    assert fill.get_setting(fill.MARKER, db=_db()) == first
    assert _search("word4") == []

    # A restart carries on from where the last committed batch ended.
    monkeypatch.setattr(fill, "set_setting", real)
    _fill_all()

    assert fill.get_setting(fill.MARKER, db=_db())["filled"] == 5
    for seq in range(2, 7):
        assert _search(f"word{seq}") == [f"m{seq}"]


def test_a_comment_deleted_before_the_fill_is_never_indexed(chat):
    _drop_comment_index()
    _input(2, "", [_comment("c2.1", "release plan", "pelicans")])
    _input(3, "", [_comment("c3.1", "release plan", "zebras")])
    _migrate()
    db = _db()
    db.execute("DELETE FROM chat_comments WHERE user_message_id = 'm2'")
    db.commit()

    _fill_all()

    assert _search("pelicans") == []
    assert _search("zebras") == ["m3"]
    assert _db().execute("SELECT count(*) FROM comment_fts WHERE comment_fts MATCH 'release'").fetchone()[0] == 1


def test_a_comment_placed_during_the_fill_is_indexed_once(chat, monkeypatch):
    import infra.comment_search_fill as fill

    _drop_comment_index()
    for seq in range(2, 5):
        _input(seq, "", [_comment(f"c{seq}.1", "release plan", "pelicans")])
    _migrate()
    # Placed after the migration and before the fill starts: under the fill's ceiling, and
    # already indexed by the trigger.
    _input(5, "", [_comment("c5.1", "release plan", "pelicans")])
    monkeypatch.setattr(fill, "BATCH_ROWS", 1)
    assert fill.fill_batch(_db()) == fill.MORE

    _input(6, "", [_comment("c6.1", "release plan", "pelicans")])  # the trigger indexes it
    _fill_all()

    assert sorted(_search("pelicans")) == ["m2", "m3", "m4", "m5", "m6"]
    assert _db().execute("SELECT count(*) FROM comment_fts WHERE comment_fts MATCH 'pelicans'").fetchone()[0] == 5


def test_the_fill_replaces_a_row_the_trigger_already_indexed(chat):
    """A rowid under the ceiling may already be indexed; the fill rewrites it, never adds a second copy."""
    _drop_comment_index()
    _migrate()
    _input(2, "flamingos", [_comment("c2.1", "release plan", "pelicans")])
    db = _db()
    db.execute("UPDATE messages SET content = 'herons' WHERE id = 'm2'")
    db.commit()

    _fill_all()

    assert _search("herons pelicans") == ["m2"]
    assert _db().execute("SELECT count(*) FROM comment_fts WHERE comment_fts MATCH 'flamingos'").fetchone()[0] == 0


async def test_the_fill_logs_its_totals_once(chat, monkeypatch):
    import infra.comment_search_fill as fill

    _drop_comment_index()
    _input(2, "", [_comment("c2.1", "release plan", "pelicans")])
    # A comment on a seq that is no agent message gets no chat_comments row.
    _input(3, "", [_comment("c3.1", "nowhere", "zebras", seq=99)])
    _migrate()

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)
    monkeypatch.setattr("infra.db_async.run_bulk", inline)
    lines: list[str] = []
    monkeypatch.setattr(fill.log, "info", lambda message, *args: lines.append(message.format(*args)))

    await fill.run_fill()
    await fill.run_fill()  # finished: nothing filled again, nothing logged again

    assert lines == ["comment search fill done: rows filled=1 comments without a chat_comments row=1"]
    assert fill.get_setting(fill.MARKER, db=_db())["completed_at"]
