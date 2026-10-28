"""Human messages tell agents to answer with post_update (#2971).

Two parts share one hard constraint: nothing a provider has cached may change.
The footer is stamped on the row at admission, and the reminder is written into
the stored tool result, so every replay renders the same bytes.
"""
from __future__ import annotations

import json
import logging

import pytest

from infra.time import utc_now_iso

from delegation_fixtures import deleg_db  # noqa: F401
from tests.plan_execution_helpers import ProbeTool, execute_as, registry_of

FOOTER = (
    '[To reply, call post_update(text="<your reply>")]\n'
    "If no reply is needed, do not send one."
)
STAMP = {"reply_via_post_update": True}


@pytest.fixture(autouse=True)
def _forget_reminded_turns():
    """The in-process once-per-Turn guard is keyed by Turn id; tests reuse ids."""
    from tools import post_update_reminder

    post_update_reminder._reminded.clear()
    yield
    post_update_reminder._reminded.clear()


def _admit(db, thread_id, turn_id, *, content="hello", role="user", metadata=None):
    from infra.thread_driver import admit_input

    return admit_input(
        thread_id,
        role=role,
        content=content,
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch="test",
        admitted_at=utc_now_iso(),
        metadata=metadata,
        db=db,
    )


def _metadata(db, message_id):
    raw = db.execute("SELECT metadata FROM messages WHERE id=?", (message_id,)).fetchone()[0]
    return json.loads(raw) if raw else {}


# -- footer ---------------------------------------------------------------


def test_stamped_human_row_ends_with_the_exact_footer():
    from core.message_history import frame_agent_envelope, render_agent_envelope

    assert frame_agent_envelope("What broke?", STAMP) == f"What broke?\n{FOOTER}"
    render = render_agent_envelope("What broke?", STAMP)
    assert render.body_content == "What broke?"
    assert render.managed_suffix == f"\n{FOOTER}"


@pytest.mark.parametrize("metadata", [None, {}, {"plan_paused_since": None}, {"routed_event": "x"}])
def test_pre_change_human_row_renders_unchanged(metadata):
    """A row admitted before the stamp existed renders byte-identically to today."""
    from core.message_history import frame_agent_envelope

    assert frame_agent_envelope("What broke?", metadata) == "What broke?"


def test_pre_change_row_stays_plain_through_every_projection():
    from history.adapters import _native_user_message
    from history.messages import _portable_message

    row = {"id": "m1", "seq": 1, "role": "user", "content": "What broke?",
           "created_at": "t", "_meta": {}}
    message = _native_user_message(_portable_message(row, "turn-1"))
    assert message == {"role": "user", "content": "What broke?"}

    stamped = _native_user_message(
        _portable_message({**row, "_meta": dict(STAMP)}, "turn-1"),
    )
    assert stamped["content"] == f"What broke?\n{FOOTER}"


def test_agent_send_keeps_its_own_reply_line_and_no_human_footer():
    from core.message_history import frame_agent_envelope

    framed = frame_agent_envelope(
        "hi", {"sender_kind": "agent", "from_thread": "t1", "from_title": "A", **STAMP},
    )
    assert framed.endswith('[To reply, call delegate_send(thread_id="t1", message="<your reply>")]')
    assert "post_update" not in framed


def test_admission_stamps_a_human_message_only(deleg_db):
    human = _admit(deleg_db, "sender-self", "t-human")
    assert _metadata(deleg_db, human.accepted.message_id) == STAMP

    for turn, extra in (
        ("t-agent", {"sender_kind": "agent", "from_thread": "x"}),
        ("t-done", {"completion": True}),
        ("t-event", {"routed_event": "pr"}),
    ):
        admitted = _admit(deleg_db, "sender-self", turn, metadata=extra)
        assert "reply_via_post_update" not in _metadata(deleg_db, admitted.accepted.message_id)

    system = _admit(deleg_db, "sender-self", "t-sys", role="system")
    assert "reply_via_post_update" not in _metadata(deleg_db, system.accepted.message_id)


def test_retry_of_a_pre_stamp_message_keeps_its_stored_payload(deleg_db):
    first = _admit(deleg_db, "sender-self", "t-old")
    deleg_db.execute(
        "UPDATE messages SET metadata=NULL WHERE id=?", (first.accepted.message_id,),
    )  # the row as it was stored before the stamp existed
    deleg_db.commit()
    again = _admit(deleg_db, "sender-self", "t-old")
    assert again.accepted.duplicate is True
    assert _metadata(deleg_db, first.accepted.message_id) == {}


# -- reminder ---------------------------------------------------------------


def _start_human_turn(db, turn_id="turn-h"):
    """Admit a human message so it starts a Turn; return that Turn's identity."""
    from core.turn_core import TurnIdentity

    admission = _admit(db, "sender-self", turn_id)
    assert admission.batch is not None
    order = db.execute("SELECT turn_order FROM active_turns WHERE id=?", (turn_id,)).fetchone()[0]
    return TurnIdentity(turn_id, int(order))


def _settle(db, turn_id, count, content="done"):
    from infra.queries import insert_message

    for index in range(count):
        insert_message(
            "sender-self", "tool", content,
            {"turn_id": turn_id, "tool_call_id": f"c{index}", "assistant_step_id": f"s{index}",
             "name": "probe"},
            db=db,
        )
    db.commit()


async def _call(registry, identity, name="uncataloged_probe"):
    return await execute_as(registry, identity, "sender-self", name)


@pytest.mark.asyncio
async def test_reminder_fires_on_the_fifth_result_and_only_once(deleg_db, caplog):
    from tools.post_update_reminder import REMINDER, REMINDER_FIRST_LINE

    identity = _start_human_turn(deleg_db)
    registry = registry_of(ProbeTool())
    caplog.set_level(logging.INFO)

    _settle(deleg_db, identity.turn_id, 3)
    assert await _call(registry, identity) == "ok"          # four results not yet settled

    _settle(deleg_db, identity.turn_id, 1)
    fired = await _call(registry, identity)
    assert fired == f"ok\n\n{REMINDER}"
    assert 'post_update(text="<your reply>")' in REMINDER
    assert "tool-by-tool" in REMINDER and "nothing-changed" in REMINDER

    from infra.queries import insert_message

    insert_message(  # the harness stores the reminded result it was given
        "sender-self", "tool", fired,
        {"turn_id": identity.turn_id, "tool_call_id": "cx", "assistant_step_id": "sx",
         "name": "probe"},
        db=deleg_db,
    )
    deleg_db.commit()
    assert await _call(registry, identity) == "ok"
    assert REMINDER_FIRST_LINE not in await _call(registry, identity)
    assert sum("post_update.reminder_emitted" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_reminder_is_once_per_turn_even_before_the_row_is_stored(deleg_db):
    """Parallel results of one step add it once; the stored rows are not needed."""
    identity = _start_human_turn(deleg_db)
    registry = registry_of(ProbeTool())
    _settle(deleg_db, identity.turn_id, 4)
    first = await _call(registry, identity)
    second = await _call(registry, identity)
    assert first.startswith("ok\n\n[CORSIA reminder") and second == "ok"


@pytest.mark.asyncio
async def test_no_reminder_after_a_successful_post_update(deleg_db):
    from infra.queries import insert_message

    identity = _start_human_turn(deleg_db)
    _settle(deleg_db, identity.turn_id, 4)
    insert_message(
        "sender-self", "assistant", "Answered.",
        {"for_user": True, "turn_id": identity.turn_id}, db=deleg_db,
    )
    deleg_db.commit()
    assert await _call(registry_of(ProbeTool()), identity) == "ok"


@pytest.mark.asyncio
async def test_no_reminder_in_a_turn_no_human_started(deleg_db):
    from core.turn_core import TurnIdentity

    _admit(deleg_db, "sender-self", "turn-a", metadata={"sender_kind": "agent", "from_thread": "x"})
    order = deleg_db.execute("SELECT turn_order FROM active_turns WHERE id='turn-a'").fetchone()[0]
    identity = TurnIdentity("turn-a", int(order))
    _settle(deleg_db, "turn-a", 6)
    assert await _call(registry_of(ProbeTool()), identity) == "ok"


@pytest.mark.asyncio
async def test_reminder_skips_an_error_result_and_waits_for_a_plain_one(deleg_db):
    identity = _start_human_turn(deleg_db)
    _settle(deleg_db, identity.turn_id, 4)
    broken = registry_of(ProbeTool(result="Error: nope"))
    assert (await _call(broken, identity)).startswith("Error: nope")
    assert "CORSIA reminder" not in await _call(broken, identity)
    assert "CORSIA reminder" in await _call(registry_of(ProbeTool()), identity)


@pytest.mark.asyncio
async def test_replayed_reminder_renders_unchanged(deleg_db):
    """The stored reminded result is what every later render returns, byte for byte."""
    from core.message_history import _tool_history_message
    from infra.queries import insert_message

    identity = _start_human_turn(deleg_db)
    _settle(deleg_db, identity.turn_id, 4)
    reminded = await _call(registry_of(ProbeTool()), identity)
    assert "CORSIA reminder" in reminded
    message_id = insert_message(
        "sender-self", "tool", reminded,
        {"turn_id": identity.turn_id, "tool_call_id": "call-r", "assistant_step_id": "step-r",
         "name": "probe"},
        db=deleg_db,
    )
    deleg_db.commit()
    row = dict(deleg_db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    first = _tool_history_message(row, tool_result_budget_tokens=None)["content"]
    again = _tool_history_message(row, tool_result_budget_tokens=None)["content"]
    assert first == again == reminded


def test_post_update_is_reachable_on_every_harness_surface():
    """One advertised list serves native, Claude Code and Codex; post_update rides its tools_load index."""
    from subagents.harness_tool_bridge import build_tool_registry
    from tools.catalog import is_reachable
    from tools.tool_surface import TOOL_CALL, TOOLS_LOAD, pack_index, reachable_tool_names

    registry = build_tool_registry()
    assert registry.has("post_update") and is_reachable("post_update")
    assert "post_update" in reachable_tool_names(registry)
    assert "- post_update:" in pack_index(registry, frozenset())
    from subagents.harness_tool_bridge import dynamic_tool_definitions

    advertised = {d["function"]["name"] for d in dynamic_tool_definitions(registry, frozenset())}
    assert {TOOLS_LOAD, TOOL_CALL} <= advertised


def test_guidance_names_what_is_worth_sending_and_rejects_noise():
    from tools.catalog_entries import POST_UPDATE_ATTENTION_GUIDANCE as guidance

    for phrase in ("direct answer", "material assumption", "discovery", "phase result",
                   "blocker", "changed expectation", "completion",
                   "tool-by-tool", "nothing-changed"):
        assert phrase in guidance


# -- content-free counts -----------------------------------------------------


def test_claiming_a_human_message_logs_one_content_free_human_turn(deleg_db, caplog):
    caplog.set_level(logging.INFO)
    _admit(deleg_db, "sender-self", "turn-count", content="a private question")
    lines = [r.getMessage() for r in caplog.records if "post_update.human_turn" in r.getMessage()]
    assert lines == ["post_update.human_turn thread=sender-self turn=turn-count"]
    assert "private" not in lines[0]


def test_an_agent_turn_logs_no_human_turn(deleg_db, caplog):
    caplog.set_level(logging.INFO)
    _admit(deleg_db, "sender-self", "turn-agent", metadata={"sender_kind": "agent"})
    assert not [r for r in caplog.records if "post_update.human_turn" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_successful_send_logs_one_content_free_sent_line(deleg_db, caplog, monkeypatch):
    from unittest.mock import AsyncMock

    from tools.post_update import PostUpdateTool

    identity = _start_human_turn(deleg_db, "turn-sent")
    monkeypatch.setattr("tools.post_update.broadcast_message_rows", AsyncMock())
    caplog.set_level(logging.INFO)
    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="secret answer", _host_access_turn_identity=identity,
    )
    assert result.startswith("Update saved as message ")
    lines = [r.getMessage() for r in caplog.records if "post_update.sent" in r.getMessage()]
    assert lines == ["post_update.sent thread=sender-self turn=turn-sent"]
