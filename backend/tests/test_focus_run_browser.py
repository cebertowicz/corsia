"""The browser chip a hidden Focus run's stub takes from its rows' display projection."""

from __future__ import annotations

import json

from api.thread_focus_page import page_focus
from test_thread_focus_page import _SID, _Thread, _db


def _open(url: str, call_id: str, *, show_window: bool = True) -> dict:
    args = {"url": url, **({"show_window": True} if show_window else {})}
    return {"id": call_id, "function": {"name": "browser_open", "arguments": json.dumps(args)}}


def _opened(t: _Thread, turn: str, url: str, call_id: str, *, failed: bool = False, **args) -> str:
    """A hidden step that opened `url`, with the stored result that settles it."""
    step = t.add("w", turn, meta={"llm_tool_calls": [_open(url, call_id, **args)]})
    t.add("t", turn, meta={"assistant_step_id": step, "tool_call_id": call_id, **({"is_error": True} if failed else {})})
    return step


def _runs(thread_setup) -> list[dict]:
    conn = _db()
    thread = _Thread(conn)
    thread_setup(thread)
    conn.commit()
    return page_focus(conn, _SID, 10, None)["runs"]


def test_a_visible_open_in_a_hidden_step_gives_its_run_the_chip():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "https://www.github.com/a", "c1")
        t.add("w", "T1")
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert stub["browser_chip"] == {"host": "github.com"}
    assert stub["count"] == 2


def test_a_headless_open_and_a_run_with_no_open_have_no_chip_key():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "https://a.example", "c1", show_window=False)
        t.add("s", "T1")
        t.add("w", "T2")
        t.add("s", "T2")

    headless, plain = _runs(setup)
    assert "browser_chip" not in headless and "browser_chip" not in plain


def test_a_failed_or_unsettled_open_gives_no_chip_and_a_settled_one_does():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "https://failed.example", "bad", failed=True)
        t.add("s", "T1")
        _opened(t, "T2", "https://done.example", "ok")
        t.add("s", "T2")
        t.add("w", "T3", meta={"llm_tool_calls": [_open("https://running.example", "run")]})
        t.add("s", "T3")

    failed, done, unsettled = _runs(setup)
    assert "browser_chip" not in failed
    assert done["browser_chip"] == {"host": "done.example"}
    assert "browser_chip" not in unsettled


def test_a_bridged_open_gives_the_chip():
    bridged = {"id": "c1", "function": {"name": "mcp__corsia__tool_call", "arguments": json.dumps({
        "name": "mcp__corsia__browser_open",
        "arguments": {"url": "https://bridged.example/x", "show_window": True},
    })}}

    def setup(t):
        t.add("u")
        step = t.add("w", "T1", meta={"llm_tool_calls": [bridged]})
        t.add("t", "T1", meta={"assistant_step_id": step, "tool_call_id": "c1"})
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert stub["browser_chip"] == {"host": "bridged.example"}


def test_the_last_open_of_a_run_wins_and_each_run_keeps_its_own():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "https://first.example", "c1")
        _opened(t, "T1", "https://second.example", "c2")
        t.add("s", "T1")
        _opened(t, "T2", "https://other.example", "c3")
        t.add("s", "T2")

    older, newer = _runs(setup)
    assert older["browser_chip"] == {"host": "second.example"}
    assert newer["browser_chip"] == {"host": "other.example"}


def test_an_open_on_a_said_to_you_row_does_not_leak_into_the_hidden_run_around_it():
    def setup(t):
        t.add("u")
        t.add("w", "T1")
        t.add("s", "T1", meta={"llm_tool_calls": [_open("https://visible.example", "c1")]})
        t.add("w", "T2")
        t.add("s", "T2")

    older, newer = _runs(setup)
    assert "browser_chip" not in older and "browser_chip" not in newer


def test_a_stub_never_carries_a_row_body_or_tool_calls():
    def setup(t):
        t.add("u")
        _opened(t, "T1", "https://a.example", "c1")
        t.add("s", "T1")

    (stub,) = _runs(setup)
    assert set(stub) == {
        "kind", "first_id", "last_id", "count", "turn_ids", "weight",
        "continues_older", "first_seq", "last_seq", "kinds", "browser_chip",
    }
