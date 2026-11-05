"""Messages reads against a synthetic chat.db: decoding, each fixed state, limits, untrusted-data notice."""

from __future__ import annotations

import sqlite3

import pytest

from connections.messages import account
from tests.messages_fixtures import make_chat_db, typedstream
import tools.messages_texts as texts
from tools import messages_db
from tools.messages_tools import (
    MessagesChatsTool, MessagesConnectTool, MessagesReadTool, MessagesSearchTool, limit_of,
)


@pytest.fixture
def chat_db(tmp_path, monkeypatch):
    path = make_chat_db(tmp_path / "chat.db")
    monkeypatch.setattr(messages_db, "CHAT_DB", path)
    monkeypatch.setattr(messages_db, "is_mac", lambda: True)

    async def reach(_thread_id):
        return {"id": "row-1", "state": "connected"}

    monkeypatch.setattr(account, "usable_for_thread", reach)
    return path


def test_decode_reads_the_text_column_then_the_attributed_body() -> None:
    assert messages_db.decode("plain", None) == "plain"
    assert messages_db.decode(None, typedstream("é emoji 😀 — dash")) == "é emoji 😀 — dash"
    assert messages_db.decode(None, typedstream("L" * 245)) == "L" * 245
    assert messages_db.decode(None, b"") == "" and messages_db.decode(None, b"no string here") == ""


async def test_chats_lists_newest_first_with_the_data_rule(chat_db) -> None:
    out = await MessagesChatsTool().execute(_thread_id="t")
    lines = out.splitlines()
    assert lines[0] == texts.DATA_RULE
    assert [line.split(" | ")[0] for line in lines[1:]] == ["2", "1", "3"]
    assert "3 people" in lines[1] and "1 person" in lines[2]
    assert len((await MessagesChatsTool().execute(_thread_id="t", limit=1)).splitlines()) == 2


async def test_read_is_oldest_first_skips_tapbacks_and_cuts_long_texts(chat_db) -> None:
    out = await MessagesReadTool().execute(_thread_id="t", chat_id="1")
    body = out.splitlines()[1:]
    assert [line.split(": ", 1)[1] for line in body] == ["plain text column", "rich text only in attributedBody", "reply from me"]
    assert "me: reply from me" in body[-1] and "Liked" not in out
    group = await MessagesReadTool().execute(_thread_id="t", chat_id="2")
    assert "x" * 500 + "… [100 more chars]" in group


async def test_search_matches_both_text_columns_and_cuts_at_200(chat_db) -> None:
    assert "rich text only" in await MessagesSearchTool().execute(_thread_id="t", query="ATTRIBUTEDBODY")
    out = await MessagesSearchTool().execute(_thread_id="t", query="x" * 3)
    assert "x" * 200 + "…" in out and "x" * 201 not in out.split("…")[0]
    assert await MessagesSearchTool().execute(_thread_id="t", query="nothing like this") == texts.NO_MATCH


async def test_empty_and_bad_arguments(chat_db, tmp_path, monkeypatch) -> None:
    assert await MessagesReadTool().execute(_thread_id="t", chat_id="999") == f"Error: {texts.NO_CHAT}"
    assert await MessagesReadTool().execute(_thread_id="t", chat_id="abc") == texts.NEEDS_CHAT_ID
    assert await MessagesSearchTool().execute(_thread_id="t", query=" ") == texts.NEEDS_QUERY
    empty = tmp_path / "empty.db"
    make_chat_db(empty)
    sqlite3.connect(empty).executescript("DELETE FROM chat_message_join; DELETE FROM message;")
    monkeypatch.setattr(messages_db, "CHAT_DB", empty)
    assert await MessagesChatsTool().execute(_thread_id="t") == texts.NO_CHATS
    assert await MessagesReadTool().execute(_thread_id="t", chat_id="1") == texts.NO_TEXTS


async def test_no_full_disk_access(chat_db) -> None:
    chat_db.chmod(0)
    try:
        assert await MessagesChatsTool().execute(_thread_id="t") == f"Error: {texts.NO_FDA}"
    finally:
        chat_db.chmod(0o600)


async def test_never_signed_in(chat_db) -> None:
    chat_db.unlink()
    assert await MessagesChatsTool().execute(_thread_id="t") == f"Error: {texts.NO_HISTORY}"


async def test_schema_mismatch_names_the_table_and_columns(chat_db) -> None:
    db = sqlite3.connect(chat_db)
    db.executescript("ALTER TABLE message RENAME COLUMN attributedBody TO body_blob;")
    db.close()
    expected = texts.SCHEMA.format(table="message", columns="attributedBody")
    assert await MessagesChatsTool().execute(_thread_id="t") == f"Error: {expected}"


@pytest.mark.parametrize(("error", "line"), [
    ("database is locked", texts.UNREADABLE), ("disk I/O error", texts.UNREADABLE),
    ("unable to open database file", texts.NO_FDA), ("authorization denied", texts.NO_FDA),
])
def test_a_busy_or_damaged_database_is_not_reported_as_missing_access(chat_db, monkeypatch, error, line) -> None:
    class Failing:
        def execute(self, *_args):
            raise sqlite3.OperationalError(error)

        def close(self) -> None:
            pass

    monkeypatch.setattr(messages_db.sqlite3, "connect", lambda *_a, **_k: Failing())
    with pytest.raises(messages_db.MessagesError) as raised:
        messages_db.open_db()
    assert str(raised.value) == line


async def test_a_group_chat_without_a_name_is_labelled_by_its_members(chat_db) -> None:
    db = sqlite3.connect(chat_db)
    db.execute("UPDATE chat SET display_name=NULL WHERE ROWID=2")
    db.commit()
    db.close()
    line = (await MessagesChatsTool().execute(_thread_id="t")).splitlines()[1]
    assert "chat123" not in line and "friend@example.com" in line and "+15555550101" in line
    db = messages_db.open_db()
    try:
        label = messages_db.chat_by_id(db, "2")["label"]
    finally:
        db.close()
    assert "chat123" not in label and "friend@example.com" in label and label.endswith("(group chat, 3 people)")


def test_a_handle_never_starts_with_a_dash() -> None:
    assert messages_db.normalize_handle("-e@example.com") is None
    assert messages_db.normalize_handle("Friend@Example.com") == "friend@example.com"


async def test_search_shows_the_words_that_matched_in_a_long_text(chat_db) -> None:
    db = sqlite3.connect(chat_db)
    db.execute("UPDATE message SET text=? WHERE ROWID=1", ("a" * 400 + " needle here",))
    db.commit()
    db.close()
    assert "needle here" in await MessagesSearchTool().execute(_thread_id="t", query="needle")


async def test_not_a_mac_answers_every_tool(chat_db, monkeypatch) -> None:
    monkeypatch.setattr(messages_db, "is_mac", lambda: False)
    for tool, args in ((MessagesChatsTool(), {}), (MessagesReadTool(), {"chat_id": "1"}),
                       (MessagesSearchTool(), {"query": "a"}), (MessagesConnectTool(), {})):
        assert await tool.execute(_thread_id="t", **args) == texts.NOT_A_MAC


async def test_a_thread_without_the_row_cannot_read(chat_db, monkeypatch) -> None:
    async def none(_thread_id):
        return None

    monkeypatch.setattr(account, "usable_for_thread", none)
    assert await MessagesChatsTool().execute(_thread_id="t") == f"Error: {texts.NOT_CONNECTED}"


def test_limit_is_clamped_and_defaults() -> None:
    assert (limit_of(None), limit_of(0), limit_of(500), limit_of(7), limit_of(True)) == (20, 1, 50, 7, 20)


def test_database_is_opened_read_only(chat_db) -> None:
    db = messages_db.open_db()
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM message")
    finally:
        db.close()
