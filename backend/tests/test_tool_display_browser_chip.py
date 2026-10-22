"""The browser chip of a settled, visible `browser_open`: one projection, live and on reload."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.message_display import _convert_assistant_for_display
from core.turn_core import TurnIdentity
from tools.tool_display import SettledResult, project_tool_display


def _open_args(**extra) -> dict:
    return {"url": "https://www.GitHub.com/a/b", "show_window": True, **extra}


GITHUB = {"host": "github.com"}


@pytest.mark.parametrize(("name", "arguments", "settled", "chip"), [
    ("browser_open", _open_args(), SettledResult("ok"), GITHUB),
    ("mcp__corsia__browser_open", _open_args(), SettledResult("ok"), GITHUB),
    ("browser_open", json.dumps(_open_args()), SettledResult("ok"), GITHUB),
    ("mcp__corsia__tool_call", {"name": "browser_open", "arguments": _open_args()}, SettledResult("ok"), GITHUB),
    ("tool_call", {"name": "mcp__corsia__browser_open", "arguments": _open_args()}, SettledResult("ok"), GITHUB),
    ("browser_open", _open_args(url="not a url"), SettledResult("ok"), {"host": None}),
    ("browser_open", _open_args(), SettledResult("cancelled", synthetic_reason="user_interruption"), GITHUB),
    # Running, failed and refused calls claim no chip.
    ("browser_open", _open_args(), None, None),
    ("browser_open", _open_args(), SettledResult("Error: no window", is_error=True), None),
    ("browser_open", _open_args(), SettledResult("nothing ran", is_error=True, synthetic_reason="undispatched_tool"), None),
    # A headless open puts nothing in front of anyone.
    ("browser_open", {"url": "https://a.example"}, SettledResult("ok"), None),
    ("browser_open", {"url": "https://a.example", "show_window": False}, SettledResult("ok"), None),
    ("browser_snapshot", _open_args(), SettledResult("ok"), None),
    # A bridge whose own arguments are not an object is no dispatch (resolve_dispatch passes it through).
    ("tool_call", {"name": "browser_open", "arguments": json.dumps(_open_args())}, SettledResult("ok"), None),
])
def test_chip_is_projected_once_a_visible_open_settles_unfailed(name, arguments, settled, chip):
    assert project_tool_display(name, arguments, settled).browser_chip == chip


def test_chip_rides_the_row_fields_only_when_there_is_one():
    shown = project_tool_display("browser_open", _open_args(), SettledResult("ok"))
    assert shown.fields()["browser_chip"] == GITHUB
    assert "browser_chip" not in shown.outcome_fields()
    assert "browser_chip" not in project_tool_display("bash", {"command": "ls"}, SettledResult("x")).fields()


def test_chip_reaches_the_tool_done_frame_the_finalized_step_and_reload(isolated_db):
    from core.step_handler import StepHandler
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
        "VALUES ('t-chip', 'u', 'omlx@local/test', 't', 't')"
    )
    db.commit()
    db.close()
    frames: list[dict] = []

    async def bcast(payload: dict) -> None:
        frames.append(payload)

    handler = StepHandler(
        thread_id="t-chip", model_info={"model": "test/model"}, broadcast=bcast, turn_id="turn-1",
    )
    handler.bind_turn_identity(TurnIdentity("turn-1", 1))
    arguments = json.dumps(_open_args())
    asyncio.run(handler.handle("tool_calls_start", {
        "tools": [{"id": "c1", "name": "browser_open", "hint": "", "args": arguments}],
        "content": "",
        "tool_call_dicts": [
            {"id": "c1", "type": "function", "function": {"name": "browser_open", "arguments": arguments}},
        ],
    }))
    assert "browser_chip" not in next(frame for frame in frames if frame["type"] == "tool_start")
    asyncio.run(handler.handle("tool_complete", {"id": "c1", "name": "browser_open", "result": "opened"}))
    asyncio.run(handler.handle("tool_calls_done", {"tool_ids": ["c1"]}))

    done = next(frame for frame in frames if frame["type"] == "tool_done")
    final = next(frame for frame in frames if frame["type"] == "step_text")
    assert done["browser_chip"] == GITHUB
    assert final["metadata"]["tool_calls"][0]["browser_chip"] == GITHUB

    db = get_db()
    rows = [dict(row) for row in db.execute(
        "SELECT role, content, metadata FROM messages WHERE thread_id='t-chip' ORDER BY rowid"
    ).fetchall()]
    db.close()
    step = json.loads(rows[0]["metadata"])
    results = {json.loads(row["metadata"])["tool_call_id"]: row for row in rows[1:]}
    row = {"role": "assistant", "content": "", "metadata": json.dumps({"llm_tool_calls": step["llm_tool_calls"]})}
    (call,) = json.loads(_convert_assistant_for_display(row, results)["metadata"])["tool_calls"]
    assert call["browser_chip"] == GITHUB
