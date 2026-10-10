"""The agent-facing views: the line, the seen derivation, the read, the tool.

Design §6.12 tests 8, 9, 14 (child threads) and 15, against the §3.5 wording.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from infra import presence
from infra.presence_view import (
    PresenceState, presence_line, presence_state, read_thread_presence, render_line,
)
from tests.presence_helpers import (
    USER, add_reply, at, claim, new_thread, pacific, pdb, release,
)

__all__ = ["pacific", "pdb"]


def _state(presence_name, since, reply=None, seen=None, child=False):
    return PresenceState(presence_name, since, reply, seen, child)


@pytest.mark.parametrize(("state", "as_of", "line"), [
    (_state("here", at(0)), at(minutes=25),
     "User presence (2026-09-18 14:05 PDT): here now, since 13:40 PDT."),
    (_state("away", at(minutes=11), at(minutes=12), False), at(minutes=100),
     "User presence (2026-09-18 15:20 PDT): away since 13:51 PDT (1 h 29 min); "
     "has not been here since your last reply at 13:52 PDT."),
    (_state("away", at(minutes=-1178), at(minutes=-1180), True), at(minutes=100),
     "User presence (2026-09-18 15:20 PDT): away since 2026-09-17 18:02 PDT (21 h); "
     "was here after your last reply."),
    (_state("unknown", at(minutes=11)), at(minutes=22),
     "User presence (2026-09-18 14:02 PDT): unknown until the user's browser "
     "reconnects after a server restart; last here 13:51 PDT."),
    (_state("never_opened", None), at(minutes=22),
     "User presence (2026-09-18 14:02 PDT): the user has never opened this conversation."),
])
def test_line_matches_the_design_examples_verbatim(pacific, state, as_of, line):
    assert render_line(state, as_of=as_of) == line


@pytest.mark.parametrize(("away", "text"), [
    (timedelta(minutes=59, seconds=59), "59 min"),
    (timedelta(hours=11, minutes=59), "11 h 59 min"),
    (timedelta(hours=47, minutes=59), "47 h"),
    (timedelta(days=3, hours=23), "3 d"),
])
def test_durations_round_down_by_band(pacific, away, text):
    line = render_line(_state("away", at(0)), as_of=at(0) + away)
    assert line.endswith(f"({text}).")


def test_hidden_after_asking_and_reply_in_the_grace_is_not_seen(pdb, pacific):
    thread = new_thread(pdb, "seen-hidden")
    claim(pdb, thread, at(0))
    release(pdb, at(60), reason="hidden")
    add_reply(pdb, thread, at(70))

    expected = (
        "User presence (2026-09-18 13:45 PDT): away since 13:41 PDT (4 min); "
        "has not been here since your last reply at 13:41 PDT."
    )
    assert presence_line(pdb, thread, as_of=at(minutes=5)) == expected
    presence.sweep(pdb, now=at(minutes=1, seconds=31))
    presence.sweep(pdb, now=at(minutes=4))
    assert presence.live_view(USER, thread) is None
    assert presence_line(pdb, thread, as_of=at(minutes=5)) == expected


def test_visible_again_inside_the_grace_is_here(pdb):
    thread = new_thread(pdb, "seen-back")
    claim(pdb, thread, at(0))
    release(pdb, at(60), reason="hidden")
    add_reply(pdb, thread, at(70))
    claim(pdb, thread, at(80))

    state = presence_state(pdb, thread)
    assert (state.presence, state.since, state.here_since_last_reply) == (
        "here", at(0), True,
    )


def test_back_inside_the_merge_window_is_here_since_the_original_entry(pdb):
    thread = new_thread(pdb, "seen-merge")
    claim(pdb, thread, at(0))
    release(pdb, at(60))
    add_reply(pdb, thread, at(70))
    presence.sweep(pdb, now=at(61))
    claim(pdb, thread, at(150))

    state = presence_state(pdb, thread)
    assert (state.presence, state.since, state.here_since_last_reply) == (
        "here", at(0), True,
    )


def test_a_reply_one_microsecond_after_the_user_left_is_not_seen(pdb):
    thread = new_thread(pdb, "seen-precision")
    claim(pdb, thread, at(0))
    release(pdb, at(60))
    add_reply(pdb, thread, at(60) + timedelta(microseconds=1))
    assert presence_state(pdb, thread).here_since_last_reply is False

    same = new_thread(pdb, "seen-same-instant")
    claim(pdb, same, at(0))
    release(pdb, at(60))
    add_reply(pdb, same, at(60))
    assert presence_state(pdb, same).here_since_last_reply is True


def test_no_assistant_text_means_no_reply_clause(pdb, pacific):
    thread = new_thread(pdb, "seen-no-reply")
    add_reply(pdb, thread, at(-10), content="   ")
    claim(pdb, thread, at(0))
    release(pdb, at(60))

    assert presence_line(pdb, thread, as_of=at(minutes=3)) == (
        "User presence (2026-09-18 13:43 PDT): away since 13:41 PDT (2 min)."
    )
    assert presence_state(pdb, thread).here_since_last_reply is None


def test_read_is_one_primary_key_lookup(pdb):
    thread = new_thread(pdb, "read")
    assert read_thread_presence(pdb, user_id=USER, thread_id=thread) == {
        "recorded": False, "reason": "never_opened",
    }
    claim(pdb, thread, at(0))
    release(pdb, at(60))
    presence.sweep(pdb, now=at(minutes=4))
    claim(pdb, thread, at(minutes=10))

    assert read_thread_presence(pdb, user_id=USER, thread_id=thread) == {
        "recorded": True, "present": True,
        "last_entered_at": "2026-09-18T20:50:00Z",
        "last_exited_at": "2026-09-18T20:41:00Z",
        "previous_entered_at": "2026-09-18T20:40:00Z",
    }
    plan = " ".join(
        str(step["detail"]) for step in pdb.execute(
            "EXPLAIN QUERY PLAN SELECT present, last_entered_at, last_exited_at, "
            "previous_entered_at FROM thread_presence WHERE user_id=? AND thread_id=?",
            (USER, thread),
        )
    )
    assert "USING INDEX sqlite_autoindex_thread_presence_1" in plan
    assert "(user_id=? AND thread_id=?)" in plan


def test_child_thread_never_opened_has_no_line(pdb, pacific):
    parent = new_thread(pdb, "parent")
    child = new_thread(pdb, "child", parent=parent)
    assert presence_line(pdb, child, as_of=at(0)) is None
    assert presence_line(pdb, parent, as_of=at(0)) == (
        "User presence (2026-09-18 13:40 PDT): the user has never opened this conversation."
    )

    claim(pdb, child, at(0))
    assert presence_line(pdb, child, as_of=at(5)) == (
        "User presence (2026-09-18 13:40 PDT): here now, since 13:40 PDT."
    )


def _call_tool(monkeypatch, thread_id: str, as_of, **arguments) -> str:
    import infra.time
    from tools.user_presence import UserPresenceTool

    monkeypatch.setattr(infra.time, "utc_now", lambda: as_of)
    tool = UserPresenceTool()
    return asyncio.run(tool.execute(_thread_id=thread_id, **arguments))


def test_tool_line_is_byte_equal_to_the_pushed_line(pdb, pacific, monkeypatch):
    thread = new_thread(pdb, "tool")
    claim(pdb, thread, at(0))
    release(pdb, at(minutes=11))
    add_reply(pdb, thread, at(minutes=12))
    as_of = at(minutes=100)

    result = json.loads(_call_tool(monkeypatch, thread, as_of))

    assert result == {
        "presence": "away",
        "as_of": "2026-09-18T22:20:00Z",
        "since": "2026-09-18T20:51:00Z",
        "last_reply_at": "2026-09-18T20:52:00Z",
        "here_since_last_reply": False,
        "line": presence_line(pdb, thread, as_of=as_of),
    }
    assert result["line"].encode() == presence_line(pdb, thread, as_of=as_of).encode()


def test_tool_never_opened_child_returns_null_line(pdb, monkeypatch):
    child = new_thread(pdb, "tool-child", parent=new_thread(pdb, "tool-parent"))
    add_reply(pdb, child, at(0))
    result = json.loads(_call_tool(monkeypatch, child, at(60)))
    assert (result["presence"], result["since"], result["line"]) == (
        "never_opened", None, None,
    )
    assert result["here_since_last_reply"] is False


def test_tool_refuses_any_argument_and_lives_in_the_history_pack():
    from tools.catalog import TOOL_CATALOG
    from tools.user_presence import UserPresenceTool

    assert UserPresenceTool().validate_params({"thread_id": "other"})
    assert UserPresenceTool().validate_params({}) == []
    entry = TOOL_CATALOG["user_presence"]
    assert (entry.category, entry.core) == ("history", False)
    assert entry.summary == (
        "Is the user in this conversation now; have they been here since your last reply"
    )


def test_tool_read_failure_uses_the_design_error(monkeypatch):
    import infra.presence_view
    from tools.user_presence import UserPresenceTool

    def _boom(*_args, **_kwargs):
        raise RuntimeError("locked")

    monkeypatch.setattr(infra.presence_view, "user_presence_payload", _boom)
    text = asyncio.run(UserPresenceTool().execute(_thread_id="any"))
    assert text == (
        "User presence could not be read just now (RuntimeError). Nothing "
        "changed; call user_presence again, or treat presence as unknown."
    )
