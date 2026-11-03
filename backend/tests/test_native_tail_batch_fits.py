"""One Assistant Step's batch of tool results after a checkpoint, on a local seat.

Each result is projected to the harness budget (4,096 tokens), but the newest
Step stays in the checkpoint's tail, so a few parallel reads together can be
larger than the room the fixed prompt and tools leave. Nothing older is left to
fold, so the install itself must fit the batch.
"""

from __future__ import annotations

import json

import pytest

from tests.test_native_refold_no_boundary import (  # noqa: F401 - seat is a fixture
    MODEL, READS, REASONING, TAIL_TOO_LARGE, WINDOW, _adopted_state, _outcome, _text, seat,
)

BATCH = 4  # parallel reads in the newest Step, each well over the 4,096-token preview


def _seed_batch_turn(thread: str) -> str:
    """One read, a checkpoint through it, then a Step that reads BATCH references at once."""
    from infra.database import get_db

    name, size = READS[1]  # 28,726 characters
    calls = [{"id": f"b{index}", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": f"{name}#{index}"})}}
        for index in range(BATCH)]
    rows: list[tuple[str, str, str, dict[str, object]]] = [
        ("u1", "user", "Yes, please continue with my setup.", {"turn_id": "turn-1"}),
        ("s1", "assistant", "", {"turn_id": "turn-1", "reasoning": REASONING, "llm_tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": READS[0][0]})}}]}),
        ("r1", "tool", _text(*READS[0]), {
            "turn_id": "turn-1", "tool_call_id": "c1", "assistant_step_id": f"{thread}-s1",
            "name": "read_file"}),
        ("s2", "assistant", "", {"turn_id": "turn-1", "reasoning": REASONING, "llm_tool_calls": calls}),
    ]
    rows.extend((f"b{index}", "tool", _text(name, size), {
        "turn_id": "turn-1", "tool_call_id": f"b{index}", "assistant_step_id": f"{thread}-s2",
        "name": "read_file"}) for index in range(BATCH))
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,'u',?,'t','t')",
        (thread, MODEL),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-1',?,0,?,'running','t','boot')", (thread, MODEL),
    )
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,'t',?)",
        [(f"{thread}-{m}", thread, r, c, json.dumps(md), seq) for seq, (m, r, c, md) in enumerate(rows, 1)],
    )
    checkpoint_id = f"{thread}-c1"
    db.execute(
        "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
        " mode,status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,started_at,raw_result,"
        " continuity_snapshot_json,captured_at,installed_at) "
        "VALUES (?,?,0,3,?,'pressure','cold','installed','native',?,'native',?,0,'[]','t',?,'[]','t','t')",
        (checkpoint_id, thread, f"{thread}-r1", MODEL, MODEL,
         "The person is partway through first-run setup; one reference read."),
    )
    db.commit()
    db.close()
    return checkpoint_id


@pytest.mark.asyncio
async def test_a_step_whose_tool_batch_outgrows_the_room_still_continues(seat):
    checkpoint_id = _seed_batch_turn("t1")
    state, installed_tokens = await _adopted_state("t1", checkpoint_id)

    assert installed_tokens <= WINDOW
    assert await _outcome(state, "t1", "turn-1") == "replies"
