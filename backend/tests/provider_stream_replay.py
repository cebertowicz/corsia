"""Replay a recorded provider stream through the real pump and snapshot state.

This is what a decomposition PR runs against to show behavior was preserved:
drive the production event pump with frames the provider actually sent, then
compare the durable result before and after the refactor. Nothing else in the
suite does this — the pumps' existing tests hand-author their events, so they
prove the pump handles what we imagined, not what arrives.

**Tool results are replayed the way production produces them.** Every CC tool
executes through the backend bridge, which persists the canonical result bytes
*before* the provider can echo the ``tool_result`` frame
(``harness_tool_bridge.execute_tool``). A replay that fired the recorded echo
without that write would be reproducing an orphan that cannot occur in
production, and ``bind_tool_result_echo`` would correctly reject it. So the
driver performs the bridge write first, using the recorded result content, and
then delivers the recorded echo.

The snapshot is deliberately narrow: durable rows and their order, not
timings, ids, or wall-clock stamps. A refactor is allowed to change those; it
is not allowed to change what the user ends up with.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from tests.provider_stream_fixtures import StreamFixture, normalize


@dataclass
class ReplayResult:
    """Durable state after replaying one stream, plus what the pump saw."""

    messages: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    broadcasts: list[dict] = field(default_factory=list)
    events_delivered: int = 0

    def digest(self) -> str:
        """Stable comparison key: durable content and order, nothing else."""
        return json.dumps(
            {"messages": self.messages, "turns": self.turns},
            sort_keys=True, indent=2,
        )


def seed_thread(db, thread_id: str, *, model: str = "claude-code/sonnet",
                turn_id: str | None = None, content: str = "replay"):
    """Create, admit, and provider-accept a thread Turn through its owners."""
    from core.turn_core import persist_turn_start
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        (thread_id, "user-replay", model, "t", "t"),
    )
    db.commit()
    exact_turn_id = turn_id or f"{thread_id}-turn"
    admission = admit_input(
        thread_id,
        role="user",
        content=content,
        intent="apply_now",
        message_id=f"{thread_id}-input",
        turn_id=exact_turn_id,
        boot_epoch="replay",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    identity = persist_turn_start(
        exact_turn_id, thread_id, model, content, db=db,
    )
    attempt_id = f"{thread_id}-write"
    begin_batch_write(admission.batch, attempt_id=attempt_id, started_at="t", db=db)
    settle_batch_write(attempt_id, outcome="accepted", db=db)
    db.commit()
    return identity


def build_live(identity, *, model: str = "claude-code/sonnet",
               tool_registry=None):
    """A ``_Live`` in the state the pump expects mid-turn.

    The context snapshot is not decoration: the terminal path audits usage
    through ``merge_context_usage_with_snapshot``, which refuses a usage record
    with no ``snapshot_id``. Without it every replayed stream ends with its
    usage recording failing, so the snapshot would silently omit terminal
    context state a decomposition is supposed to preserve.
    """
    from subagents.cc_runtime_state import _Live

    snapshot = {
        "snapshot_id": "ctx-replay",
        "harness_model_facts": {
            "harness": "claude-code",
            "model": model,
            "model_context_window": 200_000,
        },
    }
    return _Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="replay",
        corsia_turn=identity,
        tool_registry=tool_registry,
        turn_active=True,
        last_call_usage={"input_tokens": 100},
        context_window=200_000,
        catalog_model_id=model,
        context_snapshot_id=snapshot["snapshot_id"],
        context_snapshot=snapshot,
    )


def _persist_bridge_result(db, live, thread_id: str, event: dict) -> bool:
    """Write the canonical tool row the bridge writes before the echo arrives.

    Returns False when the recorded echo names a call this stream never
    declared — a real finding about the fixture, not something to paper over.
    """
    from core.tool_result_delivery import persist_tool_execution_result

    call_id = event.get("tool_use_id")
    step_id = live.tool_step_ids.get(call_id)
    if not call_id or not step_id:
        return False
    persist_tool_execution_result(
        thread_id=thread_id,
        turn_id=live.corsia_turn.turn_id,
        invocation_id=f"inv-{uuid.uuid4()}",
        assistant_step_id=step_id,
        provider_tool_call_id=call_id,
        name=live.tool_names.get(call_id) or "tool",
        arguments=None,
        content=event.get("content") or "",
        is_error=bool(event.get("is_error")),
        db=db,
    )
    db.commit()
    return True


def snapshot(db, thread_id: str) -> tuple[list[dict], list[dict]]:
    """Durable messages and turns for one thread, in their durable order."""
    messages = [
        {
            "seq": row["seq"],
            "role": row["role"],
            "content": row["content"],
            "delivery_state": row["delivery_state"],
            "metadata_type": (
                json.loads(row["metadata"] or "{}") or {}
            ).get("type"),
        }
        for row in db.execute(
            "SELECT seq, role, content, delivery_state, metadata FROM messages "
            "WHERE thread_id=? ORDER BY seq, id", (thread_id,),
        )
    ]
    turns = [
        {"status": row["status"], "error": row["error"]}
        for row in db.execute(
            "SELECT status, error FROM active_turns WHERE thread_id=? "
            "ORDER BY id", (thread_id,),
        )
    ]
    return messages, turns


async def replay(
    fixture: StreamFixture,
    db,
    *,
    thread_id: str = "thread-replay",
    stop_after: int | None = None,
) -> ReplayResult:
    """Drive the real pump with a recorded stream and return durable state.

    ``stop_after`` delivers only the first N events, which is how the crash
    harness interrupts a stream partway: every prefix is a state the process
    could really have died in.
    """
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE

    events = normalize(fixture)
    identity = seed_thread(db, thread_id)
    live = build_live(identity)
    _LIVE.put(thread_id, live)

    broadcasts: list[dict] = []

    def bcast(_thread_id, payload):
        broadcasts.append(payload)

    # db=None is what production's main path passes (cc_runtime.py:1108): every
    # handler write then lands on a pooled connection through run_in_db.
    # Handing the pump this test's own connection instead makes the terminal
    # path contend with itself — the handler holds a write transaction while
    # its out-of-band usage and snapshot writers block on another connection
    # until busy_timeout expires, ~11s of pure waiting per stream with no CPU
    # work. That cost would fall on every kill point once the crash harness
    # replays prefixes, so parity here is what keeps the net affordable.
    on_event = _make_on_event(live, thread_id, bcast, None)

    delivered = 0
    for event in events:
        if stop_after is not None and delivered >= stop_after:
            break
        if event.get("kind") == "tool_result":
            _persist_bridge_result(db, live, thread_id, event)
        await on_event(event)
        delivered += 1

    messages, turns = snapshot(db, thread_id)
    return ReplayResult(
        messages=messages, turns=turns, broadcasts=broadcasts,
        events_delivered=delivered,
    )
