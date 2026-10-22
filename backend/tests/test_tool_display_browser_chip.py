"""The browser chip of a visible `browser_open` that succeeded: projected once, stored on its Step as it settles."""

from __future__ import annotations

import asyncio
import json

import pytest

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
    # Only an open that succeeded: running, failed, refused and unknown calls claim no chip.
    ("browser_open", _open_args(), None, None),
    ("browser_open", _open_args(), SettledResult("Error: no window", is_error=True), None),
    ("browser_open", _open_args(), SettledResult("nothing ran", is_error=True, synthetic_reason="undispatched_tool"), None),
    ("browser_open", _open_args(), SettledResult("cancelled", synthetic_reason="user_interruption"), None),
    # A headless open puts nothing in front of anyone.
    ("browser_open", {"url": "https://a.example"}, SettledResult("ok"), None),
    ("browser_open", {"url": "https://a.example", "show_window": False}, SettledResult("ok"), None),
    ("browser_snapshot", _open_args(), SettledResult("ok"), None),
    # A bridge whose own arguments are not an object is no dispatch (resolve_dispatch passes it through).
    ("tool_call", {"name": "browser_open", "arguments": json.dumps(_open_args())}, SettledResult("ok"), None),
])
def test_chip_is_projected_for_a_visible_open_that_succeeded(name, arguments, settled, chip):
    assert project_tool_display(name, arguments, settled).browser_chip == chip


def test_chip_never_rides_a_call_row():
    shown = project_tool_display("browser_open", _open_args(), SettledResult("ok"))
    assert shown.browser_chip == GITHUB
    assert "browser_chip" not in shown.fields()


def _native_step(calls: list[tuple[str, dict]]):
    """A native Step declaring `calls`, with the frames it broadcast."""
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
    asyncio.run(handler.handle("tool_calls_start", {
        "tools": [{"id": call_id, "name": "browser_open", "hint": "", "args": json.dumps(args)} for call_id, args in calls],
        "content": "",
        "tool_call_dicts": [
            {"id": call_id, "type": "function", "function": {"name": "browser_open", "arguments": json.dumps(args)}}
            for call_id, args in calls
        ],
    }))
    return handler, frames


def _step_metadata(step_id: str) -> dict:
    from infra.database import get_db

    db = get_db()
    try:
        return json.loads(db.execute("SELECT metadata FROM messages WHERE id=?", (step_id,)).fetchone()[0])
    finally:
        db.close()


def test_native_settle_stores_the_chip_on_the_step_and_the_finalized_frame_carries_it(isolated_db):
    handler, frames = _native_step([("c1", _open_args())])
    step_id = handler.tool_step_id
    asyncio.run(handler.handle("tool_complete", {"id": "c1", "name": "browser_open", "result": "opened"}))
    asyncio.run(handler.handle("tool_calls_done", {"tool_ids": ["c1"]}))

    assert _step_metadata(step_id)["browser_chip"] == GITHUB
    done = next(frame for frame in frames if frame["type"] == "tool_done")
    final = next(frame for frame in frames if frame["type"] == "step_text")
    assert "browser_chip" not in done
    assert final["metadata"]["browser_chip"] == GITHUB
    assert "browser_chip" not in final["metadata"]["tool_calls"][0]
    # The frame carries what the row holds, so it matches a reload.
    assert final["metadata"]["browser_chip"] == _step_metadata(step_id)["browser_chip"]


def test_a_failed_or_headless_open_stores_nothing(isolated_db):
    handler, frames = _native_step([("c1", _open_args()), ("c2", {"url": "https://a.example"})])
    step_id = handler.tool_step_id
    asyncio.run(handler.handle("tool_complete", {
        "id": "c1", "name": "browser_open", "result": "Error: no window", "is_error": True,
    }))
    asyncio.run(handler.handle("tool_complete", {"id": "c2", "name": "browser_open", "result": "opened"}))
    asyncio.run(handler.handle("tool_calls_done", {"tool_ids": ["c1", "c2"]}))

    assert "browser_chip" not in _step_metadata(step_id)
    assert "browser_chip" not in next(frame for frame in frames if frame["type"] == "step_text")["metadata"]


def test_every_harness_result_write_stores_it_and_the_reloaded_step_keeps_it(isolated_db):
    """CC and Codex settle through `persist_tool_result` and reload the Step for their finalized frame."""
    from subagents.assistant_step_display import load_assistant_step_display
    from subagents.child_thread import persist_tool_result

    handler, _ = _native_step([("c1", _open_args()), ("c2", _open_args(url="https://other.example/x"))])
    step_id = handler.tool_step_id
    asyncio.run(handler.handle("tool_complete", {"id": "c1", "name": "browser_open", "result": "opened"}))
    persist_tool_result(
        "t-chip", turn_id="turn-1", tool_use_id="c2", name="browser_open", content="opened",
        assistant_step_id=step_id,
    )

    # The newer open replaces the older one, and the declared calls are untouched.
    stored = _step_metadata(step_id)
    assert stored["browser_chip"] == {"host": "other.example"}
    assert [call["id"] for call in stored["llm_tool_calls"]] == ["c1", "c2"]
    step = load_assistant_step_display("t-chip", step_id)
    metadata = step["metadata"] if isinstance(step["metadata"], dict) else json.loads(step["metadata"])
    assert metadata["browser_chip"] == {"host": "other.example"}


def test_a_call_appended_after_the_stamp_keeps_it(isolated_db):
    """The append re-reads the Step under its write lock, so it rewrites metadata with the chip in it."""
    from infra.database import get_db
    from subagents.child_thread import _append_tool_use_to_step

    handler, _ = _native_step([("c1", _open_args())])
    step_id = handler.tool_step_id
    asyncio.run(handler.handle("tool_complete", {"id": "c1", "name": "browser_open", "result": "opened"}))
    db = get_db()
    try:
        _append_tool_use_to_step(
            "t-chip", step_id, turn_id="turn-1", tool_id="c2", name="bash", arguments={"command": "ls"},
            hint=None, provider_turn_id=_step_metadata(step_id).get("provider_turn_id"), db=db,
        )
    finally:
        db.close()

    stored = _step_metadata(step_id)
    assert [call["id"] for call in stored["llm_tool_calls"]] == ["c1", "c2"]
    assert stored["browser_chip"] == GITHUB
