"""The rotating checkpoint review: which entries a checkpoint offers and how many.

Requirements RT-1 to RT-7 of the continuity retirement design: the queue is every
active entry ordered by last review or change, a batch is sized by tokens and by
the room the fork's window leaves, each verdict stamps a review time, the write
cap counts only writes, and every skip has its own logged reason.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from loguru import logger

from tests.test_checkpoint_placement import _add, _lesson, _mark, _raw, _review_setup
from tests.test_continuity_staging import (
    _capture_install,
    _connect,
    _db,
    _request,
    _running,
    _seed_thread,
    _tool_db,
)

_OLD = "2025-01-01T00:00:00+00:00"
_INSTALLED = "2026-01-01T00:00:00+00:00"


def _stamp(db, entry_id: int, when: str) -> None:
    db.execute(
        "UPDATE continuity_entries SET updated_at=? WHERE thread_id='thread-1' AND entry_id=?",
        (when, entry_id),
    )
    db.commit()


def _install(db, checkpoint_id: str) -> None:
    from infra.checkpoints import capture, install

    capture(checkpoint_id, raw_result="usable body", usage_json=None,
            captured_at=_INSTALLED, db=db)
    install(checkpoint_id, installed_at=_INSTALLED, db=db)
    db.commit()


def _offered(block: str | None) -> list[dict]:
    assert block is not None
    return json.loads(block.split("Entries:\n", 1)[1])


def _logs():
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="INFO")
    return messages, sink


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path, db, *_ = _review_setup(tmp_path, monkeypatch)
    # Start from an empty thread: the helper seeds two entries and a checkpoint
    # this file does not want. Each test opens its checkpoint after its entries.
    db.execute("DELETE FROM checkpoints")
    db.execute("DELETE FROM continuity_entries")
    db.commit()
    return path, db


def _open(db, checkpoint_id: str = "cp-1") -> None:
    """Open a checkpoint; a later one covers a later message, so it is a new one."""
    from dataclasses import replace

    from infra.checkpoints import create_running

    if checkpoint_id == "cp-1":
        _running(db)
    else:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('thread-1-m9','thread-1','user','later','t0',9)"
        )
        db.commit()
        create_running(
            replace(_request("thread-1", checkpoint_id), through_seq=9,
                    previous_generation_id="cp-1",
                    through_message_id="thread-1-m9"),
            db=db,
        )
    db.commit()


def test_queue_is_every_active_entry_oldest_reviewed_or_changed_first(
    setup, monkeypatch
) -> None:
    from core.placement_review import review_block

    _, db = setup
    monkeypatch.setattr("core.placement_answer.history", lambda *_: [])
    newest = _add(db, "facts", _lesson("newest fact"))
    step = _add(db, "next_actions", _lesson("run the next test"))
    oldest = _add(db, "decisions", {**_lesson("oldest decision"), "rationale": "r"})
    middle = _add(db, "active_constraints", _lesson("middle constraint"))
    for entry, when in ((oldest, "2025-01-01"), (middle, "2025-02-01"),
                        (step, "2025-03-01"), (newest, "2025-04-01")):
        _stamp(db, entry, f"{when}T00:00:00+00:00")
    _open(db)

    offered = _offered(review_block("cp-1", "thread-1", 20_000, db=db))

    assert [item["entry_id"] for item in offered] == [oldest, middle, step, newest]
    assert [item["kind"] for item in offered] == [
        "lesson", "user_rule", "work_state", "work_state",
    ]


def test_entries_changed_since_the_previous_install_are_not_offered(setup) -> None:
    from core.placement_review import review_block

    _, db = setup
    old = _add(db, "facts", _lesson("old fact"))
    _stamp(db, old, _OLD)
    _open(db)
    _install(db, "cp-1")
    fresh = _add(db, "facts", _lesson("written after the last checkpoint"))
    _open(db, "cp-2")

    offered = _offered(review_block("cp-2", "thread-1", 20_000, db=db))

    assert [item["entry_id"] for item in offered] == [old]
    assert fresh not in [item["entry_id"] for item in offered]


def test_only_a_selected_mark_still_current_keeps_an_entry_out(setup) -> None:
    from core.placement_review import review_block
    from infra.placement_marks import set_mark

    _, db = setup
    queued, kept, outdated = (_add(db, "facts", _lesson(text)) for text in "abc")
    for entry in (queued, kept, outdated):
        _stamp(db, entry, _OLD)
    revision = lambda entry: _mark(db, entry)["revision"]  # noqa: E731
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=queued,
             state="selected", revision=revision(queued), detail="{}")
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=kept,
             state="reaffirmed", revision=revision(kept), detail=None)
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=outdated,
             state="selected", revision=revision(outdated) - 1, detail="{}")
    db.commit()
    _open(db)

    offered = _offered(review_block("cp-1", "thread-1", 20_000, db=db))

    assert sorted(item["entry_id"] for item in offered) == sorted([kept, outdated])


def _many_facts(db, count: int) -> list[int]:
    ids = [_add(db, "facts", _lesson(f"fact {n} " + "word " * 500)) for n in range(count)]
    for position, entry in enumerate(ids):
        _stamp(db, entry, f"2025-01-{position + 1:02d}T00:00:00+00:00")
    _open(db)
    return ids


def _entry_tokens(db, thread_id: str = "thread-1") -> list[int]:
    from core.placement_review import _due_entries, _offer
    from core.token_estimation import _text_tokens

    due = _due_entries(db, "cp-1", thread_id)
    return [
        _text_tokens(json.dumps(_offer(db, thread_id, entry), ensure_ascii=False,
                                sort_keys=True), None)
        for entry in due
    ]


def test_batch_fills_oldest_first_and_stops_at_the_first_entry_that_does_not_fit(
    setup,
) -> None:
    from core.placement_review import _fixed_text, review_block
    from core.token_estimation import _text_tokens

    _, db = setup
    ids = _many_facts(db, 4)
    each = _entry_tokens(db)
    fixed = _text_tokens(_fixed_text(db, "thread-1", True), None)

    two = _offered(review_block("cp-1", "thread-1", fixed + each[0] + each[1], db=db))
    messages, sink = _logs()
    try:
        none = review_block("cp-1", "thread-1", fixed + each[0] - 1, db=db)
    finally:
        logger.remove(sink)

    assert [item["entry_id"] for item in two] == ids[:2]
    assert none is None
    assert any("placement.review_skipped" in m and "reason=no-room" in m for m in messages)


def test_batch_never_passes_the_token_cap_however_much_room_there_is(
    setup, monkeypatch
) -> None:
    from core.placement_review import review_block

    _, db = setup
    ids = _many_facts(db, 4)
    each = _entry_tokens(db)
    monkeypatch.setattr("infra.placement_marks.REVIEW_BATCH_TOKENS", each[0] + each[1])
    messages, sink = _logs()
    try:
        offered = _offered(review_block("cp-1", "thread-1", 1_000_000, db=db))
    finally:
        logger.remove(sink)

    assert [item["entry_id"] for item in offered] == ids[:2]
    batch = [m for m in messages if "placement.review_batch" in m]
    assert len(batch) == 1
    assert "entries=2" in batch[0] and "kinds=work_state=2" in batch[0]


def _checkpoint_request(window: int, instruction: str = "Summarize."):
    from core.checkpoint_types import CHECKPOINT_OUTPUT_TOKENS, ProviderCheckpointRequest

    return ProviderCheckpointRequest(
        fork_id="cp-1", thread_id="thread-1",
        target=SimpleNamespace(context_window=window),
        instruction=instruction, reasoning_effort=None, working_dir=".",
        max_output_tokens=CHECKPOINT_OUTPUT_TOKENS,
    )


@pytest.mark.asyncio
async def test_a_smaller_window_gets_a_smaller_batch_and_a_tiny_one_none(
    setup, monkeypatch
) -> None:
    from core.checkpoint_room import fork_instruction_room
    from core.placement_review import attach_review
    from core.token_estimation import _text_tokens

    path, db = setup
    _many_facts(db, 12)
    db.close()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(path)))

    sizes = {}
    for window in (1_000_000, 200_000, 100_000, 32_000):
        request = _checkpoint_request(window)
        attached = await attach_review(request)
        block = attached.instruction[len(request.instruction):]
        sizes[window] = len(_offered(block.split("\n\n", 1)[1])) if block else 0
        room = fork_instruction_room(window, request.max_output_tokens)
        assert _text_tokens(attached.instruction, None) <= max(room, _text_tokens("Summarize.", None))

    assert sizes[1_000_000] >= sizes[200_000] > sizes[100_000] > sizes[32_000] == 0


def test_fork_room_is_the_launch_reserve_less_output_and_one_tool_round_trip() -> None:
    from core.checkpoint_fit import CHECKPOINT_INSTRUCTION_BUDGET_TOKENS, soft_pressure_remaining
    from core.checkpoint_room import fork_instruction_room
    from core.checkpoint_types import CHECKPOINT_OUTPUT_TOKENS, CHECKPOINT_TOOL_ROUND_TRIP_TOKENS

    # The window that launches at exactly the instruction budget is unchanged.
    assert fork_instruction_room(200_000, CHECKPOINT_OUTPUT_TOKENS) == (
        CHECKPOINT_INSTRUCTION_BUDGET_TOKENS
    )
    assert CHECKPOINT_INSTRUCTION_BUDGET_TOKENS == 8_192
    for window in (32_000, 64_000, 100_000, 200_000, 1_000_000):
        assert fork_instruction_room(window, CHECKPOINT_OUTPUT_TOKENS) == max(
            soft_pressure_remaining(window)
            - CHECKPOINT_OUTPUT_TOKENS - CHECKPOINT_TOOL_ROUND_TRIP_TOKENS, 0,
        )
    assert fork_instruction_room(32_000, CHECKPOINT_OUTPUT_TOKENS) == 0


def test_a_checkpoint_does_not_launch_earlier_than_before() -> None:
    from core.checkpoint_fit import WARM_FORK_RESERVE_TOKENS, soft_pressure_remaining

    assert WARM_FORK_RESERVE_TOKENS == 16_384 + 8_192 + 4_096
    assert soft_pressure_remaining(200_000) == 28_672


def test_every_verdict_stamps_a_review_time_and_sends_the_entry_to_the_back(
    setup, monkeypatch
) -> None:
    from core.placement_review import review_block

    path, db = setup
    first, second, third = (_add(db, "facts", _lesson(text)) for text in "abc")
    for entry, when in ((first, "2025-01-01"), (second, "2025-02-01"), (third, "2025-03-01")):
        _stamp(db, entry, f"{when}T00:00:00+00:00")
    _open(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    kept = continuity._run(
        "thread-1", "review",
        {"entry_id": first, "verdict": "reaffirm", "reason": "freeze until Friday"}, "cp-1",
    )
    queued = continuity._run(
        "thread-1", "review", {"entry_id": second, "verdict": "select", "placement": _raw()},
        "cp-1",
    )
    db = _connect(str(path))
    unreviewed = db.execute("SELECT reviewed_at FROM continuity_entries").fetchall()
    _install(db, "cp-1")
    stamps = {
        entry: db.execute(
            "SELECT reviewed_at FROM continuity_entries WHERE entry_id=?", (entry,)
        ).fetchone()[0]
        for entry in (first, second, third)
    }
    _open(db, "cp-2")
    offered = _offered(review_block("cp-2", "thread-1", 20_000, db=db))

    assert kept == f"Staged entry {first} verdict reaffirm."
    assert queued == f"Staged entry {second} verdict select."
    assert [row[0] for row in unreviewed] == [None, None, None]
    assert stamps[first] and stamps[second] and stamps[third] is None
    # The kept entry goes behind the one nobody reviewed; the selected one waits for its write.
    assert [item["entry_id"] for item in offered] == [third, first]
    assert offered[1]["kept_before"] == "freeze until Friday"


def test_a_next_step_goes_through_the_rotation_like_any_work_state_entry(
    setup, monkeypatch
) -> None:
    path, db = setup
    keep, finish = (_add(db, "next_actions", _lesson(text)) for text in ("run the suite", "old step"))
    _open(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    kept = continuity._run(
        "thread-1", "review",
        {"entry_id": keep, "verdict": "reaffirm", "reason": "the suite still has to run"}, "cp-1",
    )
    dropped = continuity._run(
        "thread-1", "drop", {"entry_id": finish, "reason": "done"}, "cp-1",
    )

    assert kept == f"Staged entry {keep} verdict reaffirm."
    assert dropped == f"Staged entry {finish} retirement."


def test_a_lesson_is_never_kept_and_a_keep_needs_a_reason(setup, monkeypatch) -> None:
    path, db = setup
    lesson = _add(db, "decisions", {**_lesson("prefer small commits"), "rationale": "r"})
    fact = _add(db, "facts", _lesson("a fact"))
    _open(db)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    def run(**params):
        return continuity._run("thread-1", "review", params, "cp-1")

    assert run(entry_id=lesson, verdict="reaffirm", reason="why not").startswith(
        "Error: a lesson is never kept in continuity")
    assert run(entry_id=fact, verdict="reaffirm").startswith(
        "Error: verdict reaffirm requires reason")
    assert run(entry_id=fact, verdict="already", reason="because", destination="x.md",
               quote="a quote long enough").startswith(
        "Error: reason goes only with verdict reaffirm")
    assert run(entry_id=lesson, verdict="select", placement=_raw(q1="thread")).startswith(
        "Error: a lesson leaves continuity")


def test_the_write_cap_refuses_a_select_when_the_fork_answers(setup, monkeypatch) -> None:
    path, db = setup
    first, second = (_add(db, "facts", _lesson(text)) for text in ("one", "two"))
    _open(db)
    db.close()
    monkeypatch.setattr("infra.placement_marks.OUTSTANDING_CAP", 1)
    continuity = _tool_db(path, monkeypatch)

    def run(entry, **params):
        return continuity._run("thread-1", "review", {"entry_id": entry, **params}, "cp-1")

    written = run(first, verdict="select", placement=_raw())
    refused = run(second, verdict="select", placement=_raw())
    kept = run(second, verdict="reaffirm", reason="still true")

    assert written == f"Staged entry {first} verdict select."
    assert refused.startswith("Error: the write queue is full")
    assert kept == f"Staged entry {second} verdict reaffirm."


def test_at_the_cap_the_review_is_offered_without_the_write_outcome(
    setup, monkeypatch
) -> None:
    from core.placement_review import review_block
    from infra.placement_marks import outstanding_count, set_mark

    _, db = setup
    waiting, other = (_add(db, "facts", _lesson(text)) for text in ("waiting", "other"))
    monkeypatch.setattr("infra.placement_marks.OUTSTANDING_CAP", 1)
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=waiting,
             state="reaffirmed", revision=_mark(db, waiting)["revision"], detail=None)
    db.commit()
    _open(db)
    below = review_block("cp-1", "thread-1", 20_000, db=db)
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=waiting,
             state="selected", revision=_mark(db, waiting)["revision"], detail="{}")
    db.commit()
    at_cap = review_block("cp-1", "thread-1", 20_000, db=db)

    assert outstanding_count(db) == 1
    # A keep is not a write: it never counts toward the cap.
    assert 'verdict "select": write it to a file' in below
    assert "verdict select is unavailable" not in below
    assert 'verdict "select": write it to a file' not in at_cap
    assert "verdict select is unavailable" in at_cap
    assert [item["entry_id"] for item in _offered(at_cap)] == [other]


def test_every_skip_has_its_own_logged_reason(setup, monkeypatch) -> None:
    from core.placement_review import review_block

    _, db = setup
    _open(db)
    messages, sink = _logs()
    try:
        assert review_block("cp-1", "thread-1", 20_000, db=db) is None
        fact = _add(db, "facts", _lesson("a fact"))
        db.commit()
        db.execute("DELETE FROM checkpoints")
        _open(db)
        assert review_block("cp-1", "thread-1", 1, db=db) is None
        _seed_thread(db, "parent")
        db.execute("UPDATE threads SET spawned_by='parent' WHERE id='thread-1'")
        assert review_block("cp-1", "thread-1", 20_000, db=db) is None
    finally:
        logger.remove(sink)

    reasons = [m.split("reason=")[1].split()[0] for m in messages if "review_skipped" in m]
    assert reasons == ["nothing-due", "no-room", "child-thread"]
    assert fact


@pytest.mark.asyncio
async def test_a_window_that_is_not_known_skips_the_review_and_says_so(
    setup, monkeypatch
) -> None:
    from core.placement_review import attach_review

    path, db = setup
    _add(db, "facts", _lesson("a fact"))
    _open(db)
    db.close()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(path)))
    request = _checkpoint_request(None)  # type: ignore[arg-type]
    messages, sink = _logs()
    try:
        attached = await attach_review(request)
    finally:
        logger.remove(sink)

    assert attached.instruction == request.instruction
    assert any("reason=no-window" in m for m in messages)
