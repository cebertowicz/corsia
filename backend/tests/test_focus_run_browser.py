"""The browser chip a hidden Focus run's stub reads from the chip its Steps stored as their opens settled."""

from __future__ import annotations

import json

from api.thread_focus_page import page_focus
from test_thread_focus_page import _SID, _Thread, _db


def _opened(t: _Thread, turn: str, host: str | None) -> str:
    """A hidden Step whose visible open settled, with the chip the result write stored on it."""
    return t.add("w", turn, meta={"browser_chip": {"host": host}})


def _runs(thread_setup) -> list[dict]:
    conn = _db()
    thread = _Thread(conn)
    thread_setup(thread)
    conn.commit()
    return page_focus(conn, _SID, 10, None)["runs"]


def test_a_stored_chip_on_a_hidden_step_gives_its_run_the_chip():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "github.com")
        t.add("w", "T1")
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert stub["browser_chip"] == {"host": "github.com"}
    assert stub["count"] == 2


def test_a_chip_with_no_host_still_shows():
    def setup(t):
        t.add("u")
        _opened(t, "T1", None)
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert stub["browser_chip"] == {"host": None}


def test_a_run_with_no_stored_chip_has_no_chip_key_even_with_an_unstamped_open():
    """Steps from before the chip was stored keep none: the page never reads a call to guess one."""
    old_open = {"id": "c1", "function": {"name": "browser_open", "arguments": json.dumps(
        {"url": "https://old.example", "show_window": True},
    )}}

    def setup(t):
        t.add("u")
        step = t.add("w", "T1", meta={"llm_tool_calls": [old_open]})
        t.add("t", "T1", meta={"assistant_step_id": step, "tool_call_id": "c1"})
        t.add("s", "T1")
        t.add("w", "T2")
        t.add("s", "T2")

    old, plain = _runs(setup)
    assert "browser_chip" not in old and "browser_chip" not in plain


def test_the_newest_chip_of_a_run_wins_and_each_run_keeps_its_own():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "first.example")
        t.add("w", "T1")
        _opened(t, "T1", "second.example")
        t.add("w", "T1")
        t.add("s", "T1")
        _opened(t, "T2", "other.example")
        t.add("s", "T2")

    older, newer = _runs(setup)
    assert older["browser_chip"] == {"host": "second.example"}
    assert newer["browser_chip"] == {"host": "other.example"}


def test_the_newest_chip_wins_across_ten_and_more_rows():
    """The seq ordering is numeric, not text: seq 10 is newer than seq 9."""
    def setup(t):
        t.add("u")
        for _ in range(7):
            t.add("w", "T1")
        _opened(t, "T1", "nine.example")
        _opened(t, "T1", "ten.example")
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert stub["browser_chip"] == {"host": "ten.example"}


def test_a_chip_on_a_said_to_you_row_does_not_leak_into_the_hidden_run_around_it():
    def setup(t):
        t.add("u")
        t.add("w", "T1")
        t.add("s", "T1", meta={"browser_chip": {"host": "visible.example"}})
        t.add("w", "T2")
        t.add("s", "T2")

    older, newer = _runs(setup)
    assert "browser_chip" not in older and "browser_chip" not in newer


def test_unreadable_metadata_gives_no_chip_and_does_not_break_the_page():
    def setup(t):
        t.add("u")
        t.add("w", "T1")
        t.conn.execute("UPDATE messages SET metadata='{not json' WHERE id=?", (t.ids[-1],))
        t.add("s", "T1")

    for stub in _runs(setup):
        assert "browser_chip" not in stub


def test_a_stub_never_carries_a_row_body_or_tool_calls():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "a.example")
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert set(stub) == {
        "kind", "first_id", "last_id", "count", "turn_ids", "weight",
        "continues_older", "first_seq", "last_seq", "kinds", "browser_chip",
    }
