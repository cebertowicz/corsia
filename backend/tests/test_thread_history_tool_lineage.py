from __future__ import annotations

import json
import sqlite3

import pytest

from core.message_display import llm_to_display
from core.message_history import db_to_llm_history
from core.tool_lineage import ToolLineageError


SID = "thread-lineage"
CANCEL_COPY = (
    "This tool call was cancelled by user interruption. If it is still relevant "
    "after the interruption, run it again."
)


def _assistant(
    step_id: str,
    tool_ids: list[str],
    *,
    content: str = "",
    created_at: str = "2026-07-14T00:00:00Z",
) -> dict:
    return {
        "id": step_id,
        "thread_id": SID,
        "role": "assistant",
        "content": content,
        "created_at": created_at,
        "metadata": json.dumps({
            "step_type": "tool_calls" if tool_ids else "text",
            "llm_tool_calls": [
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"},
                }
                for tool_id in tool_ids
            ],
            "tool_hints": ["Bash" for _ in tool_ids],
        }),
    }


def _tool(
    row_id: str,
    tool_id: str,
    step_id: str | None,
    content: str,
    *,
    synthetic: bool = False,
    thread_id: str = SID,
    created_at: str = "2026-07-14T00:00:03Z",
) -> dict:
    meta = {"tool_call_id": tool_id, "name": "Bash"}
    if step_id is not None:
        meta["assistant_step_id"] = step_id
    if synthetic:
        meta.update({
            "synthetic_tool_result": True,
            "synthetic_reason": "user_interruption",
        })
    return {
        "id": row_id,
        "thread_id": thread_id,
        "role": "tool",
        "content": content,
        "created_at": created_at,
        "metadata": json.dumps(meta),
    }


def _user(row_id: str, content: str, created_at: str) -> dict:
    return {
        "id": row_id,
        "thread_id": SID,
        "role": "user",
        "content": content,
        "created_at": created_at,
        "metadata": None,
    }


def _sequenced(*rows: dict) -> list[dict]:
    return [{**row, "seq": index} for index, row in enumerate(rows, start=1)]


def test_late_result_projects_onto_emitting_step_without_crossing_user() -> None:
    rows = [
        _assistant("step-a", ["call-a"], content="checking"),
        _user("user-b", "change direction", "2026-07-14T00:00:01Z"),
        _assistant(
            "step-b", [], content="new direction",
            created_at="2026-07-14T00:00:02Z",
        ),
        _tool("result-a", "call-a", "step-a", "finished late"),
    ]

    display = llm_to_display(rows)

    assert [row["id"] for row in display] == ["step-a", "user-b", "step-b"]
    step_a = display[0]
    assert json.loads(step_a["metadata"])["tool_calls"][0]["result"] == "finished late"
    assert "tool_calls" not in json.loads(display[2]["metadata"])


def test_model_history_emits_late_result_immediately_after_its_step() -> None:
    rows = [
        _assistant("step-a", ["call-a"], content="checking"),
        _user("user-b", "change direction", "2026-07-14T00:00:01Z"),
        _tool("result-a", "call-a", "step-a", "finished late"),
    ]

    history = db_to_llm_history(rows)

    assert [message["role"] for message in history] == [
        "assistant", "tool", "user",
    ]
    assert history[1] == {
        "role": "tool",
        "tool_call_id": "call-a",
        "name": "Bash",
        "content": "finished late",
    }


@pytest.mark.parametrize(
    ("result_row", "match"),
    [
        (_tool("bad", "call-a", "missing", "x"), "missing assistant step"),
        (
            _tool("bad", "call-a", "step-a", "x", thread_id="other-thread"),
            "cross-thread",
        ),
        (_tool("bad", "wrong-call", "step-a", "x"), "does not declare"),
    ],
)
def test_invalid_authoritative_lineage_stays_loud_for_model_replay(
    result_row: dict, match: str,
) -> None:
    with pytest.raises(ToolLineageError, match=match):
        db_to_llm_history([_assistant("step-a", ["call-a"]), result_row])


@pytest.mark.parametrize(
    "result_row",
    [
        _tool("bad", "call-a", "missing", "x"),
        _tool("bad", "call-a", "step-a", "x", thread_id="other-thread"),
        _tool("bad", "wrong-call", "step-a", "x"),
    ],
)
def test_invalid_authoritative_lineage_degrades_in_display(result_row: dict) -> None:
    """Display renders the thread; only the bad result's linkage is dropped.

    One malformed row previously made every ``GET /messages`` on the thread a
    500 — the thread could not be opened at all.
    """
    display = llm_to_display([_assistant("step-a", ["call-a"]), result_row])
    assert [row["id"] for row in display] == ["step-a"]


@pytest.mark.parametrize("tool_call_id", [None, "", 7])
def test_invalid_tool_call_id_stays_loud_for_model_replay(tool_call_id) -> None:
    result = _tool("bad", "call-a", "step-a", "x")
    metadata = json.loads(result["metadata"])
    metadata["tool_call_id"] = tool_call_id
    result["metadata"] = json.dumps(metadata)

    with pytest.raises(ToolLineageError, match="invalid tool_call_id"):
        db_to_llm_history([_assistant("step-a", ["call-a"]), result])


@pytest.mark.parametrize("tool_call_id", [None, "", 7])
def test_invalid_tool_call_id_degrades_in_display(tool_call_id) -> None:
    result = _tool("bad", "call-a", "step-a", "x")
    metadata = json.loads(result["metadata"])
    metadata["tool_call_id"] = tool_call_id
    result["metadata"] = json.dumps(metadata)

    display = llm_to_display([_assistant("step-a", ["call-a"]), result])
    assert [row["id"] for row in display] == ["step-a"]


def test_ambiguous_authoritative_call_definition_stays_loud_for_replay() -> None:
    assistant = _assistant("step-a", ["call-a", "call-a"])
    with pytest.raises(ToolLineageError, match="ambiguous"):
        db_to_llm_history([
            assistant,
            _tool("result-a", "call-a", "step-a", "x"),
        ])


def test_ambiguous_authoritative_call_definition_degrades_in_display() -> None:
    assistant = _assistant("step-a", ["call-a", "call-a"])
    display = llm_to_display([
        assistant,
        _tool("result-a", "call-a", "step-a", "x"),
    ])
    assert [row["id"] for row in display] == ["step-a"]


def test_legacy_result_without_step_id_uses_stable_tool_id_pairing() -> None:
    display = llm_to_display([
        _assistant("step-a", ["call-a"]),
        _tool("result-a", "call-a", None, "legacy"),
    ])
    assert json.loads(display[0]["metadata"])["tool_calls"][0]["result"] == "legacy"


def test_legacy_tool_call_shape_reconstructs_model_call_and_result() -> None:
    assistant = _assistant("step-a", [])
    assistant["metadata"] = json.dumps({
        "tool_calls": [{
            "id": "legacy-call",
            "name": "Read",
            "args": '{"file_path":"/tmp/a"}',
        }],
    })

    history = db_to_llm_history([
        assistant,
        _tool("legacy-result", "legacy-call", None, "contents"),
    ])

    assert [message["role"] for message in history] == ["assistant", "tool"]
    assert history[0]["tool_calls"] == [{
        "id": "legacy-call",
        "type": "function",
        "function": {
            "name": "Read",
            "arguments": '{"file_path":"/tmp/a"}',
        },
    }]
    assert history[1]["tool_call_id"] == "legacy-call"
    assert history[1]["content"] == "contents"


def test_real_result_wins_over_structurally_synthetic_duplicate() -> None:
    display = llm_to_display([
        _assistant("step-a", ["call-a"]),
        _tool("synthetic", "call-a", "step-a", CANCEL_COPY, synthetic=True),
        _tool("real", "call-a", "step-a", "real output"),
    ])
    assert json.loads(display[0]["metadata"])["tool_calls"][0]["result"] == "real output"


def test_real_result_equal_to_cancellation_copy_remains_real() -> None:
    display = llm_to_display([
        _assistant("step-a", ["call-a"]),
        _tool("real", "call-a", "step-a", CANCEL_COPY),
    ])
    tool_call = json.loads(display[0]["metadata"])["tool_calls"][0]
    assert tool_call["result"] == CANCEL_COPY
    assert tool_call.get("synthetic_tool_result") is not True


def test_connection_rebuild_keeps_atomic_pair_before_intervening_user() -> None:
    from core.thread_history import rebuild_messages_from_connection

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, "
        "content TEXT, metadata TEXT, created_at TEXT, "
        "delivery_state TEXT NOT NULL DEFAULT 'delivered', seq INTEGER)"
    )
    rows = _sequenced(
        _assistant("step-a", ["call-a"], content="checking"),
        _user("user-b", "change direction", "2026-07-14T00:00:01Z"),
        _tool("result-a", "call-a", "step-a", "finished late"),
    )
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) "
        "VALUES (:id, :thread_id, :role, :content, :metadata, :created_at, :seq)",
        rows,
    )

    history = rebuild_messages_from_connection(SID, 100, db=db)

    assert [message["role"] for message in history] == [
        "assistant", "tool", "user",
    ]
    assert history[1]["content"] == "finished late"


def test_prepare_history_never_merges_later_text_into_dangling_tool_step() -> None:
    from core.thread_history import prepare_history_for_llm

    messages = [
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        },
        {"role": "assistant", "content": "later text"},
    ]

    prepared = prepare_history_for_llm(messages)

    assert [message["role"] for message in prepared] == [
        "assistant", "tool", "assistant",
    ]
    assert prepared[0]["content"] == "calling"
    assert prepared[0]["tool_calls"][0]["id"] == "call-1"
    assert prepared[1]["tool_call_id"] == "call-1"
    assert prepared[2] == {"role": "assistant", "content": "later text"}
