"""A Turn that ends with nothing the person can see in Focus nudges once; the nudged Turn's words show.

The nudge is queued in the terminal write, once per person message, only for a
completed Turn with Focus on. At the nudged Turn's end its last words become the
reply unless they are exactly NO_REPLY. A Stop records no Turn end, and a Turn
the person stopped never earns a nudge later.
"""

from __future__ import annotations

import json

import pytest

from automation.plan_liveness_terminal import on_thread_turn_complete
from automation.turn_end_effects import record_turn_end
from infra.reply_not_visible_wake import EVENT, nudge_text

pytestmark = pytest.mark.focus_nudge

_THREAD = "nudge-thread"
_AT = "2026-11-03T06:19:00+00:00"


def _nudges(db) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT id,content,metadata,delivery_state FROM messages WHERE thread_id=? AND role='system' "
        "AND json_extract(metadata,'$.event_type')=?", (_THREAD, EVENT))]


def _for_user(db, message_id: str) -> bool:
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (message_id,)).fetchone()
    return json.loads(row["metadata"] or "{}").get("for_user") is True


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
        (_THREAD, "user-1", "oMLX/qwen", "t0", "t0"),
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _take_in(db, turn="t1", message="m1", **metadata) -> None:
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        _THREAD, role="user", content="Say hi in two words", intent="apply_now",
        message_id=message, turn_id=turn, boot_epoch="boot-1", admitted_at=_AT,
        metadata=metadata or None, db=db,
    )
    begin_batch_write(admission.batch, attempt_id=f"w-{turn}", started_at=_AT, db=db)
    settle_batch_write(f"w-{turn}", outcome="accepted", db=db)
    db.commit()


def _say(db, text: str, turn="t1", **metadata) -> str:
    from infra.queries import insert_message

    message_id = insert_message(_THREAD, "assistant", text, {"turn_id": turn, **metadata}, db=db)
    db.commit()
    return message_id


def _take_nudge(db, turn: str) -> None:
    (nudge,) = _nudges(db)
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES (?,?,0,'oMLX/qwen','running',?,'boot-1')",
        (turn, _THREAD, _AT),
    )
    # As a claim's write settles it (``infra.input_sequencing``): delivered at the next seq.
    (seq,) = db.execute("SELECT next_message_seq FROM threads WHERE id=?", (_THREAD,)).fetchone()
    db.execute(
        "UPDATE messages SET active_turn_id=?,delivery_state='delivered',seq=? WHERE id=?",
        (turn, seq, nudge["id"]),
    )
    db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (seq + 1, _THREAD))
    db.commit()


async def _end(db, turn="t1", *, outcome="completed", error=None, successor=None, **more):
    from infra.thread_driver import finish_turn

    finish_turn(turn, outcome=outcome, ended_at=_AT, successor_turn_id=successor,
                boot_epoch="boot-1", error=error, db=db)
    db.commit()
    return await record_turn_end(
        on_thread_turn_complete, _THREAD, outcome=outcome, error=error, turn_id=turn, **more,
    )


def test_the_text_is_the_approved_wording() -> None:
    assert nudge_text("1:19", "Hi! Just checking in") == (
        'No reply is visible to the user after their 1:19 message. Your last words, which they '
        'did not see: "Hi! Just checking in" If you intended to send a visible response, please '
        "use post_update now. If no reply to their last message was necessary or expected, DO NOT "
        "SEND AN UPDATE. Instead, answer with exactly NO_REPLY and nothing else."
    )
    assert nudge_text("1:19", None) == (
        "No reply is visible to the user after their 1:19 message. If you intended to send a "
        "visible response, please use post_update now. If no reply to their last message was "
        "necessary or expected, DO NOT SEND AN UPDATE. Instead, answer with exactly NO_REPLY "
        "and nothing else."
    )


@pytest.mark.asyncio
async def test_a_turn_that_ends_with_only_hidden_words_queues_one_nudge(db, monkeypatch) -> None:
    import infra.reply_not_visible_wake as wake

    monkeypatch.setattr(wake, "_clock", lambda created_at: "1:19")
    _take_in(db)
    _say(db, "Hi! Just checking in")

    effects = await _end(db)

    (nudge,) = _nudges(db)
    assert nudge["content"] == f"[EVENT: {EVENT}]\nSummary: " + nudge_text("1:19", "Hi! Just checking in")
    assert json.loads(nudge["metadata"])["person_message_id"] == "m1"
    assert effects.drives.count(_THREAD) == 1


def test_the_clock_is_twelve_hour_with_no_am_pm_on_the_host_zone(monkeypatch) -> None:
    import time

    from infra.reply_not_visible_wake import _clock

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert _clock("2026-11-03T06:19:00+00:00") == "10:19"
        assert _clock("2026-11-03T08:05:00Z") == "12:05"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


@pytest.mark.asyncio
async def test_a_turn_with_no_text_at_all_nudges_without_the_quote(db) -> None:
    _take_in(db)

    await _end(db)

    (nudge,) = _nudges(db)
    assert "Your last words" not in nudge["content"]


@pytest.mark.asyncio
async def test_a_sent_reply_queues_nothing(db) -> None:
    _take_in(db)
    _say(db, "Hi there", for_user=True)

    effects = await _end(db)

    assert _nudges(db) == []
    assert _THREAD not in effects.drives


@pytest.mark.asyncio
async def test_focus_off_queues_nothing(db) -> None:
    from infra.settings import set_setting

    set_setting("features", {"focus_mode": False}, db=db)
    db.commit()
    _take_in(db)
    _say(db, "Hi")

    await _end(db)

    assert _nudges(db) == []


@pytest.mark.asyncio
async def test_an_error_queues_no_nudge(db) -> None:
    _take_in(db)
    _say(db, "Hi")

    await _end(db, outcome="error", error="provider exploded")

    assert _nudges(db) == []


@pytest.mark.asyncio
async def test_a_message_from_an_agent_queues_nothing(db) -> None:
    _take_in(db, sender_kind="agent", from_thread="other")
    _say(db, "Noted")

    await _end(db)

    assert _nudges(db) == []


@pytest.mark.asyncio
async def test_a_later_turn_does_not_nudge_the_same_message_again(db) -> None:
    _take_in(db)
    _say(db, "Hi")
    await _end(db)
    _take_nudge(db, "t2")  # It says nothing, so a repeat nudge would carry the same words.

    effects = await _end(db, turn="t2")

    assert len(_nudges(db)) == 1
    assert _THREAD not in effects.drives


@pytest.mark.asyncio
async def test_a_new_person_message_gets_its_own_nudge(db) -> None:
    _take_in(db)
    await _end(db)
    _take_nudge(db, "t2")
    await _end(db, turn="t2")
    _take_in(db, turn="t3", message="m2")

    await _end(db, turn="t3")

    assert len(_nudges(db)) == 2


@pytest.mark.asyncio
async def test_a_stopped_turn_never_earns_a_nudge_later(db) -> None:
    from infra.thread_driver import finish_turn

    _take_in(db)
    _say(db, "Working on it")
    finish_turn("t1", outcome="cancelled", ended_at=_AT, successor_turn_id=None,
                boot_epoch="boot-1", db=db)
    db.commit()
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('t2',?,0,'oMLX/qwen','running',?,'boot-1')", (_THREAD, _AT),
    )
    db.commit()

    await _end(db, turn="t2")

    assert _nudges(db) == []


@pytest.mark.asyncio
async def test_a_queued_person_message_holds_the_nudge(db) -> None:
    from infra.thread_driver import admit_input

    _take_in(db)
    admit_input(
        _THREAD, role="user", content="also this", intent="queue_after_turn", message_id="m2",
        turn_id=None, boot_epoch="boot-1", admitted_at=_AT, db=db,
    )
    db.commit()

    await _end(db, successor="t2")

    assert _nudges(db) == []


@pytest.mark.asyncio
async def test_a_later_turn_already_answering_holds_the_nudge(db) -> None:
    from infra.thread_driver import finish_turn

    # An external harness records its end after the terminal, when a successor
    # may already have taken in the person's next message.
    _take_in(db)
    _say(db, "Hi")
    finish_turn("t1", outcome="completed", ended_at=_AT, successor_turn_id=None,
                boot_epoch="boot-1", db=db)
    db.commit()
    _take_in(db, turn="t2", message="m2")

    await record_turn_end(on_thread_turn_complete, _THREAD, outcome="completed", turn_id="t1")

    assert _nudges(db) == []


@pytest.mark.asyncio
async def test_the_nudged_turns_hidden_words_become_its_reply(db) -> None:
    _take_in(db)
    _say(db, "Portland: EST or PST?")
    await _end(db)
    _take_nudge(db, "t2")
    hidden = _say(db, "Portland: EST or PST?", turn="t2")

    await _end(db, turn="t2")

    assert _for_user(db, hidden)
    assert db.execute(
        "SELECT kind FROM thread_updates WHERE source_id=?", (hidden,),
    ).fetchone()["kind"] == "marked"


@pytest.mark.asyncio
async def test_the_shown_row_reaches_an_open_client(db, monkeypatch) -> None:
    import infra.state as state

    sent: list[dict] = []
    monkeypatch.setattr(state, "broadcast_to_thread", lambda thread_id, payload, **_: sent.append(payload))
    _take_in(db)
    await _end(db)
    _take_nudge(db, "t2")
    hidden = _say(db, "Here you go", turn="t2")

    await _end(db, turn="t2")

    assert any(
        payload.get("message", {}).get("id") == hidden
        and payload["message"].get("metadata", {}).get("for_user") is True
        for payload in sent
    )


@pytest.mark.parametrize("answer", ["NO_REPLY", "  NO_REPLY\n"])
@pytest.mark.asyncio
async def test_no_reply_stays_hidden(db, answer) -> None:
    _take_in(db)
    await _end(db)
    _take_nudge(db, "t2")
    hidden = _say(db, answer, turn="t2")

    await _end(db, turn="t2")

    assert not _for_user(db, hidden)


@pytest.mark.asyncio
async def test_a_nudged_turn_that_errors_still_shows_its_words(db) -> None:
    _take_in(db)
    await _end(db)
    _take_nudge(db, "t2")
    hidden = _say(db, "Which devices do you use?", turn="t2")

    await _end(db, turn="t2", outcome="error", error="provider exploded")

    assert _for_user(db, hidden)


@pytest.mark.asyncio
async def test_a_nudged_turn_that_sends_shows_nothing_extra(db) -> None:
    _take_in(db)
    await _end(db)
    _take_nudge(db, "t2")
    _say(db, "Hi there", turn="t2", for_user=True)
    note = _say(db, "Sent.", turn="t2")

    await _end(db, turn="t2")

    assert not _for_user(db, note)


@pytest.mark.asyncio
async def test_a_failure_in_the_nudge_never_loses_the_turn_end(db, monkeypatch) -> None:
    import infra.reply_not_visible_wake as wake

    def boom(*_args, **_kwargs):
        raise RuntimeError("bad row")

    monkeypatch.setattr(wake, "_newest_person_input", boom)
    _take_in(db)

    effects = await _end(db)

    assert _nudges(db) == []
    assert effects is not None
