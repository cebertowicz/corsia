"""We never claim "no result" over a result we already executed and stored.

2026-08-14/15 production. The tool runs on THIS machine: ``execute_tool``
persists the exact bytes before returning them, and Claude Code's echo is only
it repeating our own output back. But settlement asked "has CC echoed?" — the
ownership map is keyed by CC's ``tool_use_id``, which CC never sends to our
bridge — and on "no" it wrote a PERMANENT row saying "Claude Code did not
provide a result for this tool call".

The echo then arrived, found that row already bound to its call id, refused
("provider tool-result echo does not identify one canonical result"), and the
refusal escaped the stream reader, which kills claude. That ended the turn with
its remaining calls unanswered, which authored the next stub. 301 such stubs
across 74 threads; 40 reader crashes in one log. Each collision manufactured
the cause of the next.

Three independent breaks in that loop, one test each.
"""

from __future__ import annotations

import json

import pytest


def _seed_step(db, thread_id: str, step_id: str, calls: list[dict]) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (thread_id, "user-1", "claude-code/sonnet", "t0", "t0"),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            step_id,
            thread_id,
            "assistant",
            "step",
            json.dumps({"tool_calls": calls}),
            "t1",
            1,
        ),
    )


def _seed_executed_result(db, thread_id: str, step_id: str, seq: int) -> None:
    """A REAL result: executed here, persisted under our own execution id."""
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            f"exec-row-{seq}",
            thread_id,
            "tool",
            "real bytes",
            json.dumps(
                {
                    "assistant_step_id": step_id,
                    "tool_call_id": f"exec-{seq}",
                    "provider_tool_call_id": f"exec-{seq}",
                    "name": "bash",
                }
            ),
            "t2",
            seq,
        ),
    )


def test_a_step_whose_results_we_hold_owes_nothing(isolated_db):
    """The count, not the echo, decides whether a stub is owed."""
    from infra.database import get_db
    from subagents.cc_event_tool_settlement import _unheld_result_counts

    db = get_db()
    thread_id, step_id = "thread-held", "step-1"
    _seed_step(
        db,
        thread_id,
        step_id,
        [
            {"id": "toolu_1", "name": "bash", "arguments": {}},
            {"id": "toolu_2", "name": "bash", "arguments": {}},
        ],
    )
    _seed_executed_result(db, thread_id, step_id, 2)
    _seed_executed_result(db, thread_id, step_id, 3)
    db.commit()

    owed = _unheld_result_counts(thread_id, step_ids={step_id}, db=db)

    assert owed[step_id] == 0, (
        "both declared calls were executed and stored here; claiming Claude "
        "Code owes them is the false statement that starts the loop"
    )


def test_a_step_missing_one_result_owes_exactly_one(isolated_db):
    """Counting must not swing the other way and hide a genuine hole."""
    from infra.database import get_db
    from subagents.cc_event_tool_settlement import _unheld_result_counts

    db = get_db()
    thread_id, step_id = "thread-partial", "step-1"
    _seed_step(
        db,
        thread_id,
        step_id,
        [
            {"id": "toolu_1", "name": "bash", "arguments": {}},
            {"id": "toolu_2", "name": "bash", "arguments": {}},
        ],
    )
    _seed_executed_result(db, thread_id, step_id, 2)
    db.commit()

    owed = _unheld_result_counts(thread_id, step_ids={step_id}, db=db)

    assert owed[step_id] == 1


def test_a_real_result_beats_a_stub_when_both_exist_unbound(isolated_db):
    """The exact production pair, and it is why the echo refused.

    One call on one Step carrying BOTH rows, neither bound::

        provider_missing_result   binding_state = (none)
        REAL                      binding_state = (none)

    Ranking by binding state cannot separate them — neither is bound — so the
    echo saw two candidates and refused, and the refusal killed the session.
    The stub must lose to the real result on identity, not on state.
    """
    from core.tool_result_binding import _bind_echo
    from infra.database import get_db

    db = get_db()
    thread_id, step_id = "thread-pair", "step-1"
    _seed_step(
        db,
        thread_id,
        step_id,
        [{"id": "toolu_1", "name": "bash", "arguments": {}}],
    )
    for row_id, content, extra in (
        (
            "stub-row",
            "Claude Code did not provide a result for this tool call.",
            {
                "is_error": True,
                "synthetic_tool_result": True,
                "synthetic_reason": "provider_missing_result",
                "tool_invocation_id": "synthetic:provider_missing_result:toolu_1",
            },
        ),
        ("real-row", "real bytes", {}),
    ):
        db.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                row_id,
                thread_id,
                "tool",
                content,
                json.dumps(
                    {
                        "assistant_step_id": step_id,
                        "tool_call_id": "toolu_1",
                        "provider_tool_call_id": "toolu_1",
                        "name": "bash",
                        **extra,
                    }
                ),
                "t2",
                2 if row_id == "stub-row" else 3,
            ),
        )
    db.commit()

    result = _bind_echo(
        db=db,
        thread_id=thread_id,
        provider_tool_call_id="toolu_1",
        assistant_step_id=step_id,
        echoed_content="real bytes",
    )

    assert result is not None
    assert "did not provide a result" not in (result.content or ""), (
        "the echo must bind the REAL bytes, never the placeholder"
    )


@pytest.mark.asyncio
async def test_an_unbindable_result_does_not_kill_the_session(monkeypatch):
    """Drive the REAL read loop: one unplaceable result must not end claude."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from subagents.cc_stream import CCStreamSession

    killed = {"proc": False}
    seen: list[dict] = []

    class _Stdout:
        def __init__(self):
            self._lines = [b'{"type":"tool_result"}\n', b'{"type":"after"}\n', b""]

        async def readline(self):
            return self._lines.pop(0)

    class _Proc:
        returncode = None
        stdout = _Stdout()
        stderr = None

        def kill(self):
            killed["proc"] = True

        async def wait(self):
            return 0

    async def _on_event(ev):
        seen.append(ev)
        if ev.get("type") == "tool_result":
            raise ToolResultBindingError(
                "provider tool-result echo does not identify one canonical result"
            )

    monkeypatch.setattr(
        "subagents.cc_stream.normalize_event", lambda raw: [raw]
    )

    stream = CCStreamSession(binary="unused", session_id="thread-alive")
    stream.session_id = "thread-alive"
    stream._proc = _Proc()
    stream._on_event = _on_event
    stream._closing = True  # skip the exit callback; we are testing the loop
    stream._terminal_event = None
    stream._control_responses = {}

    await stream._read_loop()

    assert killed["proc"] is False, (
        "a binding failure must never terminate Claude Code — that is the step "
        "that turns one bad row into an errored turn and the next stub"
    )
    assert {ev.get("type") for ev in seen} == {"tool_result", "after"}, (
        "the reader must keep delivering events after the unplaceable one"
    )
