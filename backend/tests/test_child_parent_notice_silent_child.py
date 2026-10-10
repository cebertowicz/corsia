"""Matrix row F8: a worker's running Turn has said and called nothing for too long.

Section F's other rows fire when the worker's Turn *ends*; this one fires
while it is still running, on the continuation runner's clock. Shares the
lineage fixture with ``test_child_parent_notice_dead_child.py``.
"""

from __future__ import annotations

from automation.continuation.sources.silent_child import SilentChildSource
from infra.input_admission import accept_thread_input
from plans import delegation_child_lifecycle as lifecycle, public, service
from tests.child_parent_notice_helpers import begin_worker_turn, db, delegated, wakes
from tests.plan_writer_authorizer import raw_plan_seed

__all__ = ["db"]

#: Far past anything the fixture writes with the real clock.
NOW = "2100-01-01T00:00:00+00:00"
_TEN_MINUTES_AGO = "2099-12-31T23:50:00+00:00"
_HOUR_AGO = "2099-12-31T23:00:00+00:00"


def _sweep(db, now: str = NOW) -> list[str]:
    return SilentChildSource().prepare(now, db=db)


def _count_writes(monkeypatch) -> list[int]:
    """Count the plan writes a sweep opens (#2748: none unless someone is silent)."""
    opened: list[int] = []
    real = public.plan_transaction

    def counting(db):
        opened.append(1)
        return real(db)

    monkeypatch.setattr(public, "plan_transaction", counting)
    return opened


def _model_call(db, at: str, usage_id: str = "usage-1") -> None:
    db.execute(
        "INSERT INTO llm_usage (id,thread_id,model,created_at) VALUES (?,'worker','m',?)",
        (usage_id, at),
    )


def _message(db, at: str) -> None:
    db.execute(
        """INSERT INTO messages (id,thread_id,seq,role,content,created_at)
           VALUES ('worker-msg','worker',
                   (SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE thread_id='worker'),
                   'assistant','working',?)""",
        (at,),
    )


def _tool_message(db, at: str) -> None:
    """Record sequenced tool output produced within the worker's transcript."""
    db.execute(
        """INSERT INTO messages (id,thread_id,seq,role,content,created_at)
           VALUES ('worker-tool','worker',
                   (SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE thread_id='worker'),
                   'tool','completed',?)""",
        (at,),
    )


def _inbound_message(
    db, at: str, *, role: str = "system", message_id: str = "inbound-message",
) -> None:
    """Queue a real recipient input: it is not worker-authored output."""
    accept_thread_input(
        "worker",
        role=role,
        content="Parent is checking in.",
        intent="apply_now",
        message_id=message_id,
        metadata={"event_type": "plan.child_silent"},
        created_at=at,
        db=db,
    )


def test_f8_silent_running_worker_wakes_its_parent_once(db):
    delegated(db)
    begin_worker_turn(db)

    assert _sweep(db) == ["parent"]
    assert _sweep(db) == []

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_silent:turn-1:")
    content = str(appended[0]["content"])
    assert "Delegate work" in content
    assert "since 2000-01-01 00:00 UTC" in content
    assert "delegate_status" in content and "replace_spawned_thread" in content
    assert wakes(db, "worker") == []


def test_f8_inbound_message_does_not_reset_worker_silence(db):
    delegated(db)
    begin_worker_turn(db)
    _inbound_message(db, _TEN_MINUTES_AGO)
    _inbound_message(
        db, _TEN_MINUTES_AGO, role="user", message_id="inbound-user-message",
    )

    assert _sweep(db) == ["parent"]
    assert _sweep(db) == []
    assert len(wakes(db)) == 1


def test_f8_recent_model_call_or_message_is_not_silence(db):
    delegated(db)
    begin_worker_turn(db)
    _model_call(db, _TEN_MINUTES_AGO)
    assert _sweep(db) == []

    _model_call(db, _HOUR_AGO, "usage-0")
    _message(db, _TEN_MINUTES_AGO)
    db.execute("DELETE FROM llm_usage WHERE id='usage-1'")
    assert _sweep(db) == []

    db.execute("DELETE FROM messages WHERE id='worker-msg'")
    _tool_message(db, _TEN_MINUTES_AGO)
    assert _sweep(db) == []
    assert wakes(db) == []


def test_f8_turn_start_counts_as_progress(db):
    delegated(db)
    begin_worker_turn(db, started_at="2099-12-31T23:55:00")

    assert _sweep(db) == []


def test_f8_turn_waiting_to_retry_is_not_silent(db):
    delegated(db)
    begin_worker_turn(db)
    db.execute(
        "UPDATE active_turns SET status='waiting_retry', retry_at=? WHERE id='turn-1'",
        (NOW,),
    )

    assert _sweep(db) == []
    assert wakes(db) == []


def test_f8_settled_delegating_step_hears_nothing(db):
    step, _plan, _node = delegated(db)
    begin_worker_turn(db)
    service.transition_node(step.id, "failed", db=db)

    assert _sweep(db) == []
    assert wakes(db) == []


def test_f8_running_worker_still_wakes_while_parent_reviews(db):
    step, _plan, _node = delegated(db)
    begin_worker_turn(db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='review_requested' WHERE id=?", (step.id,))

    assert _sweep(db) == ["parent"]


def test_f8_progress_then_new_silence_is_a_new_episode(db):
    delegated(db)
    begin_worker_turn(db)
    assert _sweep(db, "2099-12-31T00:00:00+00:00") == ["parent"]

    _model_call(db, "2099-12-31T01:00:00+00:00")
    assert _sweep(db, "2099-12-31T01:10:00+00:00") == []
    assert _sweep(db) == ["parent"]

    ids = [str(row["id"]) for row in wakes(db)]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_f8_threshold_is_twenty_minutes(db):
    assert lifecycle.SILENT_AFTER_SECONDS == 1200
    delegated(db)
    begin_worker_turn(db, started_at="2099-12-31T23:41:00")
    assert _sweep(db) == []
    db.execute("UPDATE active_turns SET started_at='2099-12-31T23:40:00'")

    assert _sweep(db) == ["parent"]


def test_f8_sweep_with_no_silent_worker_takes_no_writer(db, monkeypatch):
    opened = _count_writes(monkeypatch)
    delegated(db)
    begin_worker_turn(db, started_at="2099-12-31T23:55:00")

    assert _sweep(db) == []
    assert opened == []


def test_f8_episode_already_told_takes_no_writer_on_later_sweeps(db, monkeypatch):
    opened = _count_writes(monkeypatch)
    delegated(db)
    begin_worker_turn(db)

    assert _sweep(db) == ["parent"]
    assert len(opened) == 1
    assert _sweep(db) == []
    assert _sweep(db) == []
    assert len(opened) == 1
    assert len(wakes(db)) == 1


def test_f8_runner_sweep_without_a_connection_reads_the_pooled_one(db, monkeypatch):
    """The runner's sweep passes ``db=None``; a raise here stops every trigger source."""
    from automation.continuation.sources import silent_child
    from infra import database

    pooled = database._NoOpClose(db)  # what production's get_db hands out
    monkeypatch.setattr(silent_child, "get_db", lambda: pooled, raising=False)
    monkeypatch.setattr(database, "get_db", lambda: pooled)
    delegated(db)
    begin_worker_turn(db, started_at="2099-12-31T23:55:00")
    db.commit()
    assert _sweep(None) == []

    db.execute("UPDATE active_turns SET started_at='2099-12-31T23:00:00'")
    db.commit()
    assert _sweep(None) == ["parent"]
    assert len(wakes(db)) == 1


def test_f8_write_rechecks_the_candidates_it_was_given(db):
    delegated(db)
    begin_worker_turn(db)
    candidates = lifecycle.silent_children(db, now=NOW)
    assert [str(row["turn_id"]) for row in candidates] == ["turn-1"]
    _model_call(db, _TEN_MINUTES_AGO)

    with public.plan_transaction(db) as connection:
        lifecycle.notify_silent_children(connection, now=NOW, turn_ids=["turn-1"])

    assert wakes(db) == []
