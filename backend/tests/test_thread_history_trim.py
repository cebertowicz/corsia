
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.thread_history import _trim_to_window



def _user(content: str = "u") -> dict:
    return {"role": "user", "content": content}


def _asst(content: str = "a", tool_calls: list | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _tool(tc_id: str = "t1", name: str = "x", content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tc_id, "name": name, "content": content}



def test_empty_messages_returns_empty():
    assert _trim_to_window([], 100) == []


def test_single_user_message_within_window():
    msgs = [_user("hi")]
    assert _trim_to_window(msgs, 100) == [_user("hi")]


def test_short_history_within_window_is_returned_intact():
    msgs = [_user("q1"), _asst("a1"), _user("q2"), _asst("a2")]
    out = _trim_to_window(msgs, 100)
    assert out == msgs


def test_window_smaller_than_history_picks_user_anchor():
    msgs = [
        _user("old"),
        _asst("old-a"),
        _user("recent"),
        _asst("recent-a"),
    ]
    out = _trim_to_window(msgs, 2)
    assert out == [_user("recent"), _asst("recent-a")]


def test_trailing_dangling_tool_call_assistant_is_stripped():
    msgs = [
        _user("q"),
        _asst("a", tool_calls=[{"id": "t1", "function": {"name": "x"}}]),
    ]
    out = _trim_to_window(msgs, 100)
    assert out == [_user("q")]


def test_assistant_with_paired_tool_result_is_kept():
    msgs = [
        _user("q"),
        _asst("a", tool_calls=[{"id": "t1", "function": {"name": "x"}}]),
        _tool("t1"),
    ]
    out = _trim_to_window(msgs, 100)
    assert out == msgs



def test_long_agent_workstream_350_msg_session_returns_user_anchored_history():
    msgs = [_user("kick off the long task")]
    for i in range(349):
        if i % 2 == 0:
            msgs.append(_asst(
                f"step-{i}",
                tool_calls=[{"id": f"t{i}", "function": {"name": "x"}}],
            ))
        else:
            msgs.append(_tool(f"t{i - 1}", name="x", content=f"r{i}"))

    out = _trim_to_window(msgs, 100)

    user_roles = [m for m in out if m.get("role") == "user"]
    assert len(user_roles) >= 1, (
        "Production bug regression: long agent work-streams returned "
        "[] when the last `window` messages had no user-role entries."
    )
    assert out[0]["role"] == "user"


def test_long_agent_workstream_1008_msg_session_returns_user_anchored_history():
    msgs = [_user("session opener")]
    for i in range(4):  # 4 asst/tool steps
        msgs.append(_asst(f"opener-step-{i}"))
    msgs.append(_user("real task: do the thing"))
    for i in range(1003):
        if i % 2 == 0:
            msgs.append(_asst(
                f"work-step-{i}",
                tool_calls=[{"id": f"w{i}", "function": {"name": "x"}}],
            ))
        else:
            msgs.append(_tool(f"w{i - 1}", name="x", content=f"r{i}"))

    out = _trim_to_window(msgs, 100)

    user_roles = [m for m in out if m.get("role") == "user"]
    assert len(user_roles) >= 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "real task: do the thing"


def test_no_user_messages_at_all_returns_empty():
    msgs = [_asst("a1"), _asst("a2"), _tool("t1")]
    assert _trim_to_window(msgs, 100) == []


def test_user_inside_window_is_preferred_over_earlier_anchor():
    msgs = [_user("very-old")]
    for i in range(100):
        msgs.append(_asst(f"a{i}"))
    msgs.append(_user("recent"))
    msgs.append(_asst("a-recent"))

    out = _trim_to_window(msgs, 5)

    assert out[0]["role"] == "user"
    assert out[0]["content"] == "recent"
    assert "very-old" not in [m.get("content") for m in out]


def test_trailing_dangling_assistant_stripped_after_expansion():
    msgs = [_user("q")]
    for i in range(50):
        msgs.append(_asst(f"a{i}"))
    msgs.append(_asst(
        "trailing-dangling",
        tool_calls=[{"id": "tx", "function": {"name": "x"}}],
    ))

    out = _trim_to_window(msgs, 10)

    assert out[0]["role"] == "user"
    last = out[-1]
    has_dangling = (
        last.get("role") == "assistant"
        and last.get("tool_calls")
        and last.get("content") == "trailing-dangling"
    )
    assert not has_dangling



def test_window_zero_returns_empty_or_user_anchor_only():
    msgs = [_user("q"), _asst("a")]
    out = _trim_to_window(msgs, 0)
    if out:
        assert out[0]["role"] == "user"


def test_window_equals_history_length_returns_full_history():
    msgs = [_user("q1"), _asst("a1"), _user("q2"), _asst("a2")]
    out = _trim_to_window(msgs, len(msgs))
    assert out == msgs


def test_messages_without_role_key_treated_as_non_user():
    msgs = [_user("q"), {"content": "no-role"}, _asst("a")]
    out = _trim_to_window(msgs, 100)
    assert out[0]["role"] == "user"
