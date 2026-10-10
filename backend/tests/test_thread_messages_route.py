"""The authorized page read: ownership and the page share one snapshot.

A permission check taken before the read's ``BEGIN`` can authorize a thread the
user loses in between — revoked, re-owned — and then page it from the older
reading. These pin that the check runs inside the snapshot, and that a refusal
leaves no transaction behind on the connection it was handed.
"""

from __future__ import annotations

import sqlite3

import api.thread_paging as paging

_SID = "sess-route"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages ("
        "id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, seq INTEGER, active_turn_id TEXT, "
        "input_order INTEGER, delivery_state TEXT NOT NULL DEFAULT 'delivered', "
        "delivery_intent TEXT)"
    )
    conn.execute(
        "CREATE TABLE active_turns ("
        "id TEXT, thread_id TEXT, turn_order INTEGER, status TEXT)"
    )
    conn.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES ('m1',?,'user','hello','2026-06-12T00:00:01',10)",
        (_SID,),
    )
    conn.commit()
    return conn


def test_ownership_is_checked_inside_the_page_snapshot(monkeypatch):
    conn = _db()
    seen: list[bool] = []

    def _spy(thread_id, user_id, *, db=None):
        seen.append(bool(db.in_transaction))
        return {"id": thread_id, "user_id": user_id}

    monkeypatch.setattr(paging, "get_thread_for_user", _spy)
    result = paging.read_authorized_page(conn, _SID, 50, None, user_id="u1")

    assert seen == [True]
    assert result is not None
    page, has_more, _oldest_id = result
    assert [row["id"] for row in page] == ["m1"]
    assert has_more is False
    assert len(result) == 3


def test_a_thread_the_user_does_not_own_returns_no_page(monkeypatch):
    conn = _db()
    monkeypatch.setattr(paging, "get_thread_for_user", lambda *a, **k: None)

    assert paging.read_authorized_page(conn, _SID, 50, None, user_id="u2") is None
    assert conn.in_transaction is False