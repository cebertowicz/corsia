from __future__ import annotations

import pytest


def _context(*, db, broadcasts, ordering=None):
    from core.turn_core import TurnIdentity
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_runtime_state import _Live

    identity = TurnIdentity("cc-step-turn", 1)
    live = _Live(
        session=None, parent_thread_id=None, name="cc", corsia_turn=identity,
    )
    live.turn_active = True

    def broadcast(thread_id, payload):
        if ordering is not None:
            ordering.append("broadcast")
        broadcasts.append((thread_id, payload))

    return CCEventContext(
        live=live, child_id="cc-step-child", bcast=broadcast, db=db,
    ), identity


def _event(identity):
    return {
        "_corsia_turn_identity": identity,
        "text": "Persist this step.",
        "reasoning": "",
        "tool_uses": [],
    }


def test_detached_step_persistence_and_projection_share_one_connection(monkeypatch):
    from subagents import cc_event_steps

    class Connection:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    connection = Connection()
    calls: list[tuple[str, object]] = []

    def persist(*_args, **kwargs):
        calls.append(("persist", kwargs["db"]))
        return "step-1"

    def load(*_args, **kwargs):
        calls.append(("load", kwargs["db"]))
        return {"content": "Persist this step.", "metadata": {}, "created_at": "now"}

    monkeypatch.setattr(cc_event_steps, "get_db", lambda: connection)
    monkeypatch.setattr(cc_event_steps, "persist_assistant_step", persist)
    monkeypatch.setattr(cc_event_steps, "load_assistant_step_display", load)

    message_id, projection = cc_event_steps._persist_and_load_assistant_step(
        "cc-step-child", turn_id="cc-step-turn", content="Persist this step.",
        tool_uses=[], reasoning=None, reasoning_provenance=None, model=None,
        expected_model=None, provider_message_id=None,
    )

    assert message_id == "step-1"
    assert projection["created_at"] == "now"
    assert calls == [("persist", connection), ("load", connection)]
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_detached_assistant_step_submits_one_db_job_before_broadcast(monkeypatch):
    from subagents import cc_event_steps

    broadcasts: list[tuple[str, dict]] = []
    ordering: list[str] = []
    ctx, identity = _context(db=None, broadcasts=broadcasts, ordering=ordering)
    jobs: list[tuple[object, tuple, dict]] = []

    async def one_db_job(owned_db, fn, *args, **kwargs):
        ordering.append("db")
        jobs.append((owned_db, fn, args, kwargs))
        return "step-1", {
            "content": "Persist this step.", "metadata": {}, "created_at": "now",
        }

    monkeypatch.setattr(cc_event_steps, "run_in_db_unless_owned", one_db_job)

    await cc_event_steps.on_assistant_step(ctx, _event(identity))

    assert len(jobs) == 1
    assert jobs[0][0] is None
    assert jobs[0][1].func is cc_event_steps._persist_and_load_assistant_step
    assert broadcasts[0][1]["message_id"] == "step-1"
    assert ordering == ["db", "broadcast"]


@pytest.mark.asyncio
async def test_explicit_step_connection_stays_synchronous(monkeypatch):
    from subagents import cc_event_steps

    connection = object()
    broadcasts: list[tuple[str, dict]] = []
    ctx, identity = _context(db=connection, broadcasts=broadcasts)
    calls: list[tuple[str, object]] = []

    def persist(*_args, **kwargs):
        calls.append(("persist", kwargs["db"]))
        return "step-1"

    def load(*_args, **kwargs):
        calls.append(("load", kwargs["db"]))
        return {"content": "Persist this step.", "metadata": {}, "created_at": "now"}

    monkeypatch.setattr(cc_event_steps, "persist_assistant_step", persist)
    monkeypatch.setattr(cc_event_steps, "load_assistant_step_display", load)

    await cc_event_steps.on_assistant_step(ctx, _event(identity))

    assert calls == [("persist", connection), ("load", connection)]
    assert broadcasts[0][1]["message_id"] == "step-1"
