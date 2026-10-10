from __future__ import annotations

import json

from core.message_display import llm_to_display


def _tool_calls_row(mid, ts, *, tc_id="tc1"):
    return {
        "id": mid,
        "rowid": int(mid.split("-")[-1]),
        "role": "assistant",
        "content": "",
        "created_at": ts,
        "metadata": json.dumps({
            "step_type": "tool_calls",
            "tool_calls": [{"id": tc_id, "name": "Bash", "hint": "Bash", "args": "", "result": None}],
        }),
    }


class TestTerminalToolStepProjection:
    def test_terminal_dangling_tool_calls_row_carries_source_identity(self):
        """#701: a turn whose latest step is tool_calls (never followed by a
        text row) must preserve that physical Assistant Step's created_at/id/
        rowid in the display projection.
        """
        rows = [
            {
                "id": "m-1", "rowid": 1, "role": "user", "content": "do a thing",
                "created_at": "2026-06-12T00:00:0000", "metadata": None,
            },
            _tool_calls_row("m-2", "2026-06-12T00:00:0001", tc_id="tc1"),
        ]

        display = llm_to_display(rows)

        assert len(display) == 2
        terminal = display[1]
        assert terminal["created_at"] == "2026-06-12T00:00:0001"
        assert terminal["id"] == "m-2"
        assert terminal["rowid"] == 2

    def test_distinct_terminal_tool_steps_never_merge(self):
        rows = [
            _tool_calls_row("m-10", "2026-06-12T00:00:0010", tc_id="tc-a"),
            _tool_calls_row("m-11", "2026-06-12T00:00:0011", tc_id="tc-b"),
        ]

        display = llm_to_display(rows)

        assert [row["id"] for row in display] == ["m-10", "m-11"]
        assert [
            json.loads(row["metadata"])["tool_calls"][0]["id"]
            for row in display
        ] == ["tc-a", "tc-b"]


def test_history_serializes_legacy_dict_tool_arguments_to_json() -> None:
    """A legacy call stored with dict args replays as the JSON string providers require."""
    from core.message_history import db_to_llm_history

    rows = [{
        "id": "a1",
        "role": "assistant",
        "content": "",
        "metadata": {"tool_calls": [{"id": "c1", "name": "read", "args": {"path": "x"}}]},
    }]

    messages = db_to_llm_history(rows)

    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "x"}'
