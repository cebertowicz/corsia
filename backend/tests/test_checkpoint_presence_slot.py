"""The install-frozen presence line after the mid-Turn resume anchor (#2043).

Design §3.3 and §6.12 test 11: the line is frozen at install beside the
responsibility map, and rendered only where the resume anchor is.
"""

from __future__ import annotations

import json

import pytest

from history.adapters import MID_TURN_RESUME_ANCHOR, mid_turn_resume_text, to_native_history
from infra import presence
from infra.checkpoint_receipts import frozen_presence_line, resume_anchor_text
from infra.presence_view import presence_line
from tests.presence_helpers import add_turn, at, claim, new_thread, pacific, pdb, release

__all__ = ["pacific", "pdb"]


@pytest.fixture
def log_lines():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lines.append, format="{level}|{message}", level="DEBUG")
    yield lines
    logger.remove(sink)


INSTALLED_AT = at(minutes=30)


def _messages(db, thread, rows) -> None:
    for turn in dict.fromkeys(turn for _role, turn in rows):
        add_turn(db, thread, f"{thread}-{turn}")
    for seq, (role, turn) in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"{thread}-m{seq}", thread, role, f"{role} {seq}",
             json.dumps({"turn_id": f"{thread}-{turn}"}), presence.iso(at(seconds=seq)), seq),
        )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (len(rows) + 1, thread))
    db.commit()


def _install(db, thread, *, through_seq, installed_at=INSTALLED_AT):
    from core.checkpoint_types import CheckpointRequest
    from infra.checkpoints import capture, create_running, install

    checkpoint_id = f"cp-{thread}-{through_seq}"
    create_running(CheckpointRequest(
        id=checkpoint_id, thread_id=thread, history_epoch=0,
        previous_generation_id=None, through_seq=through_seq,
        through_message_id=f"{thread}-m{through_seq}", trigger="pressure",
        mode="warm", source_harness="native", source_model="native/test",
        writer_harness="native", writer_model="native/test",
        originating_turn_id="turn-1", first_included_seq=1, started_at="t1",
    ), db=db)
    capture(checkpoint_id, raw_result="summary", usage_json='{"input_tokens":1}',
            captured_at="t2", db=db)
    return install(checkpoint_id, installed_at=presence.iso(installed_at), db=db)


def _away_thread(db, name, rows):
    thread = new_thread(db, name)
    _messages(db, thread, rows)
    claim(db, thread, at(0))
    release(db, at(60), reason="hidden")
    presence.sweep(db, now=at(minutes=5))
    return thread


MID_TURN = [("user", "turn-1"), ("assistant", "turn-1"),
            ("assistant", "turn-1"), ("assistant", "turn-1")]
TWO_TURNS = [("user", "turn-1"), ("assistant", "turn-1"),
             ("user", "turn-2"), ("assistant", "turn-2")]


def _line(db, thread):
    return presence_line(db, thread, as_of=INSTALLED_AT)


def test_install_freezes_the_line_at_the_install_time_with_the_user_away(pdb, pacific):
    thread = _away_thread(pdb, "freeze", MID_TURN)
    expected = _line(pdb, thread)
    assert "away since" in expected

    record = _install(pdb, thread, through_seq=2)
    receipt = json.loads(pdb.execute(
        "SELECT metadata FROM messages WHERE id=?", (f"checkpoint-receipt:{record.id}",),
    ).fetchone()["metadata"])
    assert receipt["resume_presence_line"] == expected
    assert "responsibility_map_snapshot" in receipt

    # The user returns after the install: the frozen value does not move.
    claim(pdb, thread, at(minutes=90), session="s2")
    assert frozen_presence_line(pdb, record.id) == expected
    assert _line(pdb, thread) != presence_line(pdb, thread, as_of=at(minutes=91))


def test_unreadable_presence_timestamp_and_receipt_metadata_warn(pdb, log_lines):
    from infra.checkpoint_receipts import _presence_line_at

    thread = new_thread(pdb, "corrupt-receipt")
    checkpoint_id = "cp-corrupt"
    assert _presence_line_at(pdb, thread, checkpoint_id, "not-a-timestamp") is None
    pdb.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"checkpoint-receipt:{checkpoint_id}", thread, "system", "Context saved", "{", "t", 1),
    )
    assert frozen_presence_line(pdb, checkpoint_id) is None

    warnings = "\n".join(line for line in log_lines if line.startswith("WARNING|"))
    assert f"thread_id={thread} checkpoint_id={checkpoint_id}" in warnings
    assert "unreadable presence timestamp" in warnings
    assert "unreadable metadata" in warnings


def test_malformed_stored_reply_time_warns_without_aborting_install(pdb, pacific, log_lines):
    thread = _away_thread(pdb, "corrupt-reply", MID_TURN)
    # `created_at` is historic receipt input, independent from installed_at.
    pdb.execute("UPDATE messages SET created_at='not-a-timestamp' WHERE id=?", (f"{thread}-m4",))
    pdb.commit()

    record = _install(pdb, thread, through_seq=2)

    assert frozen_presence_line(pdb, record.id) is None
    assert pdb.execute(
        "SELECT 1 FROM messages WHERE id=?", (f"checkpoint-receipt:{record.id}",),
    ).fetchone() is not None
    warnings = "\n".join(line for line in log_lines if line.startswith("WARNING|"))
    assert f"unreadable presence data thread_id={thread} checkpoint_id={record.id}" in warnings


def test_native_mid_turn_resume_renders_the_anchor_then_exactly_that_line(pdb, pacific):
    from core.checkpoint_resume import installed_replay

    thread = _away_thread(pdb, "native", MID_TURN)
    expected = _line(pdb, thread)
    record = _install(pdb, thread, through_seq=2)

    messages = to_native_history(installed_replay(record, db=pdb).tail)

    assert messages[0] == {
        "role": "user", "content": f"{MID_TURN_RESUME_ANCHOR}\n{expected}",
    }


def test_native_replay_prefix_is_byte_identical_across_later_turns(pdb, pacific):
    from core.checkpoint_resume import installed_replay

    thread = _away_thread(pdb, "cache", MID_TURN)
    record = _install(pdb, thread, through_seq=2)
    first = json.dumps(to_native_history(installed_replay(record, db=pdb).tail))

    claim(pdb, thread, at(minutes=120), session="s2")  # presence changes; the slot must not
    second = json.dumps(to_native_history(installed_replay(record, db=pdb).tail))

    assert first == second


def test_tail_opening_at_a_turn_start_renders_no_slot(pdb, pacific):
    from core.checkpoint_resume import installed_replay

    thread = _away_thread(pdb, "turn-start", TWO_TURNS)
    record = _install(pdb, thread, through_seq=2)
    assert frozen_presence_line(pdb, record.id)

    rendered = json.dumps(to_native_history(installed_replay(record, db=pdb).tail))

    assert MID_TURN_RESUME_ANCHOR not in rendered
    assert "User presence" not in rendered


def test_a_frozen_none_renders_the_bare_anchor():
    assert mid_turn_resume_text(None) == MID_TURN_RESUME_ANCHOR
    assert mid_turn_resume_text("") == MID_TURN_RESUME_ANCHOR


def test_claude_code_and_codex_resume_frames_carry_the_same_text(pdb, pacific):
    from core.checkpoint_resume import installed_replay

    thread = _away_thread(pdb, "external", MID_TURN)
    expected = _line(pdb, thread)
    record = _install(pdb, thread, through_seq=2)

    text = resume_anchor_text(installed_replay(record, db=pdb).tail.resume_presence_line)

    assert text == f"{MID_TURN_RESUME_ANCHOR}\n{expected}"


@pytest.mark.asyncio
async def test_external_resume_entry_points_pass_the_frozen_text(pdb, pacific, monkeypatch):
    """Both harness entry points hand the anchor-plus-line to their resume frame."""
    from types import SimpleNamespace

    from subagents import cc_resume_transition, codex_turn_transitions

    thread = _away_thread(pdb, "entry", MID_TURN)
    expected = f"{MID_TURN_RESUME_ANCHOR}\n{_line(pdb, thread)}"
    _install(pdb, thread, through_seq=2)
    # The prepared selection is authoritative even if an install races later.
    prepared = SimpleNamespace(
        replay=SimpleNamespace(tail=SimpleNamespace(resume_presence_line=_line(pdb, thread))),
    )
    seen: dict[str, str] = {}

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    live = SimpleNamespace(lock=lambda _thread: Lock())

    async def cc_locked(_thread, _live, _prepared, anchor, **_kw):
        seen["cc"] = anchor
        raise RuntimeError("stop")

    async def codex_locked(_thread, _live, _prepared, *, current_user_text, **_kw):
        seen["codex"] = current_user_text
        raise RuntimeError("stop")

    monkeypatch.setattr(cc_resume_transition, "_LIVE", live)
    monkeypatch.setattr(
        cc_resume_transition, "replace_same_turn_after_checkpoint_locked", cc_locked,
    )
    with pytest.raises(RuntimeError, match="stop"):
        await cc_resume_transition.resume_same_turn_after_checkpoint(thread, None, prepared)

    from subagents import codex_runtime

    monkeypatch.setattr(codex_runtime, "_LIVE", live)
    monkeypatch.setattr(
        codex_turn_transitions, "resume_same_turn_after_checkpoint_locked", codex_locked,
    )
    with pytest.raises(RuntimeError, match="stop"):
        await codex_turn_transitions.resume_same_turn_after_checkpoint(thread, None, prepared)

    assert seen == {"cc": expected, "codex": expected}


def test_fit_estimate_includes_the_line(pdb, pacific):
    from core.checkpoint_fit import render_cold_fold_input
    from core.checkpoint_resume import installed_replay

    thread = _away_thread(pdb, "fit", MID_TURN)
    expected = _line(pdb, thread)
    record = _install(pdb, thread, through_seq=2)
    with_line = installed_replay(record, db=pdb).tail
    assert with_line.resume_presence_line == expected

    from dataclasses import replace

    bare = replace(with_line, resume_presence_line=None)
    fitted = render_cold_fold_input(with_line, max_input_tokens=10_000)
    plain = render_cold_fold_input(bare, max_input_tokens=10_000)

    text = "\n".join(message["content"] for message in fitted.messages)
    assert f"{MID_TURN_RESUME_ANCHOR}\n{expected}" in text
    assert fitted.input_tokens > plain.input_tokens
