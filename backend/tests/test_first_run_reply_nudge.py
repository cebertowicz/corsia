"""The first-run event shares the Focus reply nudge lifecycle."""

import json

import pytest

from test_reply_not_visible_wake import (
    _AT, _THREAD, _end, _for_user, _nudges, _say, _stop, _take_nudge, db,
)

pytestmark = [pytest.mark.focus_nudge, pytest.mark.asyncio]


def _first_run(db, *, event="onboarding.first_run", local_download=False):
    from events.delivery import enqueue_event_wake
    from infra.onboarding_events import first_run_message_id, enqueue_first_run_wake
    from onboarding.first_run_opening import seed_first_model_opening

    db.execute("UPDATE threads SET model=NULL WHERE id=?", (_THREAD,))
    if event == "onboarding.first_run":
        enqueue_first_run_wake(_THREAD, first_run_path="app", db=db)
        seed_first_model_opening(_THREAD, db=db)
        event_id = first_run_message_id(_THREAD)
    else:
        event_id = "other-event"
        enqueue_event_wake(_THREAD, event, "Notice", message_id=event_id, db=db)
    event_ids = [event_id]
    if local_download:
        enqueue_event_wake(_THREAD, "onboarding.local_download", "Ready", message_id="download", db=db)
        event_ids.append("download")
    db.execute("UPDATE threads SET model='oMLX/qwen' WHERE id=?", (_THREAD,))
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('t1',?,0,'oMLX/qwen','running',?,'boot-1')", (_THREAD, _AT),
    )
    (seq,) = db.execute("SELECT next_message_seq FROM threads WHERE id=?", (_THREAD,)).fetchone()
    for item in event_ids:
        db.execute("UPDATE messages SET active_turn_id='t1',delivery_state='delivered',seq=? WHERE id=?", (seq, item))
        seq += 1
    db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (seq, _THREAD))
    db.commit()
    return event_id


async def test_first_run_with_visible_greeting_and_download_nudges_once_and_shows_fallback(db):
    anchor = _first_run(db, local_download=True)
    opening = db.execute("SELECT id,seq FROM messages WHERE id=?", (f"first-run-opening:{_THREAD}",)).fetchone()
    assert opening and _for_user(db, opening["id"])
    assert opening["seq"] < db.execute("SELECT seq FROM messages WHERE id=?", (anchor,)).fetchone()[0]
    hidden = _say(db, "Which apps do you use?")
    await _end(db)
    (nudge,) = _nudges(db)
    assert json.loads(nudge["metadata"])["person_message_id"] == anchor
    assert nudge["content"] == (
        '[EVENT: turn.reply_not_visible]\nSummary: No reply is visible to the user after setup started.'
        ' Your last words, which they did not see: "Which apps do you use?"'
        ' If you intended to send a visible response, please use post_update now.'
        ' If no reply was necessary or expected, DO NOT SEND AN UPDATE.'
        ' Instead, answer with exactly NO_REPLY and nothing else.'
    )
    _take_nudge(db, "t2")
    await _end(db, turn="t2")
    assert _for_user(db, hidden)
    assert len(_nudges(db)) == 1


@pytest.mark.parametrize("reply", ["NO_REPLY", "posted"])
async def test_first_run_nudge_respects_no_reply_and_visible_post(db, reply):
    _first_run(db)
    prior = _say(db, "Question?")
    await _end(db)
    _take_nudge(db, "t2")
    own = _say(db, "NO_REPLY" if reply == "NO_REPLY" else "Question?", turn="t2", **({"for_user": True} if reply == "posted" else {}))
    await _end(db, turn="t2")
    assert not _for_user(db, prior)
    assert _for_user(db, own) == (reply == "posted")
    assert len(_nudges(db)) == 1


@pytest.mark.parametrize("ending", ["stop", "error"])
async def test_first_run_nudge_stop_or_error_shows_prior_words(db, ending):
    from infra.turn_errors import apply_error_next_steps

    _first_run(db)
    prior = _say(db, "Question?")
    await _end(db)
    _take_nudge(db, "t2")
    if ending == "stop":
        await _stop(db, "t2")
    else:
        await _end(db, turn="t2", outcome="error", error="failed")
        row = {"id": "error", "metadata": {"type": "llm_error", "turn_id": "t2"}}
        apply_error_next_steps(db, _THREAD, [row])
        assert row["metadata"]["next_step"] == "answered"
    assert _for_user(db, prior)


@pytest.mark.parametrize("case", ["error", "stop", "focus_off", "posted", "unrelated"])
async def test_initial_exclusions(db, case, monkeypatch):
    if case == "focus_off":
        monkeypatch.setattr("infra.reply_not_visible_wake._focus_on", lambda _db: False)
    _first_run(db, event="onboarding.local_download" if case == "unrelated" else "onboarding.first_run")
    _say(db, "Question?", **({"for_user": True} if case == "posted" else {}))
    if case == "stop":
        await _stop(db, "t1")
    else:
        await _end(db, outcome="error" if case == "error" else "completed")
    assert not _nudges(db)


async def test_first_run_without_words_omits_quote_and_shows_nudge_words(db):
    _first_run(db)
    await _end(db)
    (nudge,) = _nudges(db)
    assert "Your last words" not in nudge["content"]
    _take_nudge(db, "t2")
    answer = _say(db, "Which apps?", turn="t2")
    await _end(db, turn="t2")
    assert _for_user(db, answer)


async def test_a_first_run_id_without_the_first_run_event_does_not_qualify(db):
    from infra.onboarding_events import first_run_message_id

    event_id = _first_run(db, event="plan.liveness_wake")
    db.execute("UPDATE messages SET id=? WHERE id=?", (first_run_message_id(_THREAD), event_id))
    db.commit()
    await _end(db)
    assert not _nudges(db)


async def test_queued_person_holds_first_run_nudge(db):
    from infra.thread_driver import admit_input

    _first_run(db)
    admit_input(_THREAD, role="user", content="Hello", intent="queue_after_turn", message_id="person", turn_id=None, boot_epoch="boot-1", admitted_at=_AT, db=db)
    db.commit()
    await _end(db, successor="t2")
    assert not _nudges(db)


async def test_later_person_does_not_hide_first_run_nudge_answer(db):
    from infra.reply_not_visible_wake import nudge_reply_shown
    from infra.thread_driver import admit_input
    from infra.turn_errors import apply_error_next_steps

    _first_run(db)
    _say(db, "Setup question?")
    await _end(db)
    _take_nudge(db, "t2")
    _say(db, "Setup question?", turn="t2", for_user=True)
    admit_input(_THREAD, role="user", content="Another question", intent="queue_after_turn",
                message_id="person", turn_id=None, boot_epoch="boot-1", admitted_at=_AT, db=db)
    (seq,) = db.execute("SELECT next_message_seq FROM threads WHERE id=?", (_THREAD,)).fetchone()
    db.execute("UPDATE messages SET active_turn_id='t2', delivery_state='delivered', seq=? WHERE id='person'", (seq,))
    db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (seq + 1, _THREAD))
    db.commit()
    await _end(db, turn="t2", outcome="error", error="failed")
    assert nudge_reply_shown(db, _THREAD, "t2")
    row = {"id": "error", "metadata": {"type": "llm_error", "turn_id": "t2"}}
    apply_error_next_steps(db, _THREAD, [row])
    assert row["metadata"]["next_step"] == "answered"
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('t3',?,0,'oMLX/qwen','running',?,'boot-1')", (_THREAD, _AT),
    )
    db.commit()
    await _end(db, turn="t3")
    await _end(db, turn="t3")
    assert sorted(json.loads(row["metadata"])["person_message_id"] for row in _nudges(db)) == [
        f"first-run:{_THREAD}", "person",
    ]


async def test_delivered_person_takes_priority_over_first_run(db):
    from infra.thread_driver import admit_input

    _first_run(db)
    admit_input(_THREAD, role="user", content="Hello", intent="queue_after_turn", message_id="person", turn_id=None, boot_epoch="boot-1", admitted_at=_AT, db=db)
    (seq,) = db.execute("SELECT next_message_seq FROM threads WHERE id=?", (_THREAD,)).fetchone()
    db.execute("UPDATE messages SET active_turn_id='t1', delivery_state='delivered', seq=? WHERE id='person'", (seq,))
    db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (seq + 1, _THREAD))
    db.commit()
    _say(db, "Hi")
    await _end(db)
    (nudge,) = _nudges(db)
    assert json.loads(nudge["metadata"])["person_message_id"] == "person"
    assert "after their " in nudge["content"]
