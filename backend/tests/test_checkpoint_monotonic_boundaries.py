from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from history.projection import render_portable_history
from history.turns import assistant_step_batches, turn_interior_cut_points
from history.types import HistoryBoundary


MessageRow = tuple[str, int, str, dict]


def _insert_running_turn(
    db,
    *,
    thread_id: str,
    turn_id: str,
    rows: Sequence[MessageRow],
) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'steve', 'native/test', 't', 't')",
        (thread_id,),
    )
    db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?, ?, 0, 'native/test', 'running', 't', 'boot')",
        (turn_id, thread_id),
    )
    _append_rows(db, thread_id=thread_id, turn_id=turn_id, rows=rows)
    db.commit()


def _append_rows(
    db,
    *,
    thread_id: str,
    turn_id: str,
    rows: Sequence[MessageRow],
) -> None:
    db.executemany(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?, ?, ?, ?, ?, 't', ?)",
        [
            (
                message_id,
                thread_id,
                role,
                message_id,
                json.dumps({"turn_id": turn_id, **metadata}),
                seq,
            )
            for message_id, seq, role, metadata in rows
        ],
    )


def _assistant(step_id: str, *call_ids: str) -> MessageRow:
    return (
        step_id,
        int(step_id.removeprefix("a")),
        "assistant",
        {
            "llm_tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
                for call_id in call_ids
            ]
        },
    )


def _tool(
    message_id: str,
    seq: int,
    *,
    step_id: str,
    call_id: str,
) -> MessageRow:
    return (
        message_id,
        seq,
        "tool",
        {
            "tool_call_id": call_id,
            "assistant_step_id": step_id,
            "name": "Read",
        },
    )


def test_resume_anchor_survives_a_later_group_seq_inversion(isolated_db) -> None:
    from infra.database import get_db

    db = get_db()
    thread_id = "checkpoint-boundary-inversion"
    turn_id = "turn-running"
    tool_calls = tuple(f"call-{index}" for index in range(1, 5))
    _insert_running_turn(
        db,
        thread_id=thread_id,
        turn_id=turn_id,
        rows=(
            ("u2489", 2489, "user", {}),
            _assistant("a2490", *tool_calls),
            _tool("r2491", 2491, step_id="a2490", call_id="call-1"),
            ("u2492", 2492, "user", {}),
            _tool("r2493", 2493, step_id="a2490", call_id="call-2"),
            _tool("r2494", 2494, step_id="a2490", call_id="call-3"),
            _tool("r2495", 2495, step_id="a2490", call_id="call-4"),
            _assistant("a2496"),
        ),
    )
    anchor = HistoryBoundary(thread_id, 0, 2489, "u2489")

    tail = render_portable_history(
        thread_id,
        db=db,
        start_exclusive=anchor,
        include_unfinished_turn=True,
    )

    assert [message.seq for message in tail.messages] == [
        2490,
        2491,
        2493,
        2494,
        2495,
        2492,
        2496,
    ]
    full_turn = render_portable_history(
        thread_id,
        db=db,
        include_unfinished_turn=True,
    ).turns[0]
    batches = assistant_step_batches(full_turn)
    assert batches[0].resume_anchor == anchor
    assert batches[0].end_boundary is None
    db.close()


@pytest.mark.parametrize(
    ("boundary_kind", "initial_rows", "appended_rows"),
    (
        pytest.param(
            "resume_anchor",
            (
                ("u100", 100, "user", {}),
                _assistant("a101", "call-1"),
                _tool("r102", 102, step_id="a101", call_id="call-1"),
            ),
            (
                ("u103", 103, "user", {}),
                _assistant("a104"),
            ),
            id="resume-anchor-later-answer",
        ),
        pytest.param(
            "resume_anchor",
            (
                ("u100", 100, "user", {}),
                _assistant("a101", "call-1"),
                _tool("r102", 102, step_id="a101", call_id="call-1"),
            ),
            (
                ("u103", 103, "user", {}),
                _assistant("a104", "call-2"),
                _tool("r105", 105, step_id="a104", call_id="call-2"),
            ),
            id="resume-anchor-later-tool-step",
        ),
        pytest.param(
            "interior_cut",
            (
                ("u100", 100, "user", {}),
                _assistant("a101", "call-1"),
                _tool("r102", 102, step_id="a101", call_id="call-1"),
                ("u103", 103, "user", {}),
                _assistant("a104", "call-2"),
            ),
            (
                _tool("r105", 105, step_id="a104", call_id="call-2"),
            ),
            id="interior-cut-later-tool-result",
        ),
        pytest.param(
            "interior_cut",
            (
                ("u100", 100, "user", {}),
                _assistant("a101", "call-1"),
                _tool("r102", 102, step_id="a101", call_id="call-1"),
                ("u103", 103, "user", {}),
                _assistant("a104", "call-2"),
                ("u105", 105, "user", {}),
                _assistant("a106"),
            ),
            (
                _tool("r107", 107, step_id="a104", call_id="call-2"),
            ),
            id="interior-cut-later-groups-invert",
        ),
    ),
)
def test_settled_boundary_stays_legal_when_later_groups_gain_higher_seq_rows(
    isolated_db,
    boundary_kind: str,
    initial_rows: Sequence[MessageRow],
    appended_rows: Sequence[MessageRow],
) -> None:
    from infra.database import get_db

    db = get_db()
    thread_id = f"monotonic-{boundary_kind}"
    turn_id = "turn-running"
    _insert_running_turn(
        db,
        thread_id=thread_id,
        turn_id=turn_id,
        rows=initial_rows,
    )
    initial_turn = render_portable_history(
        thread_id,
        db=db,
        include_unfinished_turn=True,
    ).turns[0]
    if boundary_kind == "resume_anchor":
        boundary = assistant_step_batches(initial_turn)[0].resume_anchor
    else:
        boundary = turn_interior_cut_points(initial_turn)[0]
    assert boundary is not None

    _append_rows(
        db,
        thread_id=thread_id,
        turn_id=turn_id,
        rows=appended_rows,
    )
    db.commit()

    tail = render_portable_history(
        thread_id,
        db=db,
        start_exclusive=boundary,
        include_unfinished_turn=True,
    )

    assert tail.start_exclusive == boundary
    assert tail.messages
    db.close()
