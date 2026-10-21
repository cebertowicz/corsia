"""The visit record: coalescing, restart, sessions, exits and history (#2004).

Design §6.12 tests 3-7 and 10, plus the §8 corrections: the merge window runs
from when an exit was detected, never from its back-dated stamp, and "Turns
while away" counts from the previous exit.
"""

from __future__ import annotations

from datetime import timedelta

from infra import presence
from infra.presence_view import presence_line, presence_state
from tests.presence_helpers import (
    add_turn, at, claim, new_thread, pacific, pdb, release, row, visits,
)

__all__ = ["pacific", "pdb"]


def test_ten_switches_in_a_minute_are_one_visit(pdb):
    thread = new_thread(pdb, "coalesce")
    for i in range(10):
        claim(pdb, thread, at(i * 6))
        release(pdb, at(i * 6 + 3))
        presence.sweep(pdb, now=at(i * 6 + 4))

    assert visits(pdb, thread) == []
    assert row(pdb, thread)["last_entered_at"] == presence.iso(at(0))
    assert presence_state(pdb, thread).presence == "away"

    presence.sweep(pdb, now=at(57) + presence.MERGE_WINDOW)

    [visit] = visits(pdb, thread)
    assert (visit["entered_at"], visit["exited_at"]) == (
        presence.iso(at(0)), presence.iso(at(57)),
    )
    assert row(pdb, thread)["previous_entered_at"] is None


def test_restart_reads_unknown_until_a_reclaim_continues_the_visit(pdb, pacific):
    thread = new_thread(pdb, "restart-reclaim")
    claim(pdb, thread, at(0))

    presence.reconcile_startup(pdb, now=at(minutes=10))
    pending = row(pdb, thread)
    assert pending["exit_pending_at"] == presence.iso(at(0))
    assert pending["exit_pending_since"] == presence.iso(at(minutes=10))
    assert presence_line(pdb, thread, as_of=at(minutes=10)) == (
        "User presence (2026-09-18 13:50 PDT): unknown until the user's browser "
        "reconnects after a server restart; last here 13:40 PDT."
    )

    # The first sweep after startup must not finalize: the stamp is 10 min old,
    # but the exit was only detected 30 s ago.
    presence.sweep(pdb, now=at(minutes=10, seconds=30))
    assert row(pdb, thread)["present"] == 1

    claim(pdb, thread, at(minutes=11), session="s2")
    continued = row(pdb, thread)
    assert (continued["present"], continued["exit_pending_at"]) == (1, None)
    assert continued["last_entered_at"] == presence.iso(at(0))
    assert presence_line(pdb, thread, as_of=at(minutes=11)) == (
        "User presence (2026-09-18 13:51 PDT): here now, since 13:40 PDT."
    )


def test_restart_without_reclaim_exits_at_the_last_confirmation(pdb, pacific):
    thread = new_thread(pdb, "restart-lost")
    claim(pdb, thread, at(0))
    presence.sweep(pdb, now=at(minutes=6))  # keep-alive confirms at 13:46
    assert row(pdb, thread)["last_confirmed_at"] == presence.iso(at(minutes=6))

    presence.reconcile_startup(pdb, now=at(minutes=20))
    presence.sweep(pdb, now=at(minutes=22))

    closed = row(pdb, thread)
    assert (closed["present"], closed["last_exited_at"]) == (
        0, presence.iso(at(minutes=6)),
    )
    assert visits(pdb, thread)[0]["exit_reason"] == "restart"
    assert presence_line(pdb, thread, as_of=at(minutes=22)) == (
        "User presence (2026-09-18 14:02 PDT): away since 13:46 PDT (16 min)."
    )


def test_two_sessions_are_one_visit_from_first_in_to_last_out(pdb):
    thread = new_thread(pdb, "two-sessions")
    claim(pdb, thread, at(0), session="phone")
    claim(pdb, thread, at(20), session="desktop")
    release(pdb, at(40), session="phone")
    presence.sweep(pdb, now=at(minutes=5))
    assert row(pdb, thread)["exit_pending_at"] is None
    assert presence_state(pdb, thread).presence == "here"

    release(pdb, at(minutes=6), session="desktop")
    presence.sweep(pdb, now=at(minutes=8))

    closed = row(pdb, thread)
    assert closed["last_entered_at"] == presence.iso(at(0))
    assert closed["last_exited_at"] == presence.iso(at(minutes=6))
    assert visits(pdb, thread)[0]["sessions"] == 2


def test_lost_socket_exits_at_release_time(pdb):
    thread = new_thread(pdb, "lost-socket")
    claim(pdb, thread, at(0))
    presence.release_session(pdb, session="s1", now=at(minutes=3))
    presence.sweep(pdb, now=at(minutes=5))

    assert row(pdb, thread)["last_exited_at"] == presence.iso(at(minutes=3))
    assert visits(pdb, thread)[0]["exit_reason"] == "disconnected"


def test_idle_exit_is_server_now_minus_idle_never_before_the_claim(pdb):
    thread = new_thread(pdb, "idle")
    claim(pdb, thread, at(0))
    release(pdb, at(minutes=15), reason="idle", idle_for=timedelta(minutes=10))
    assert row(pdb, thread)["exit_pending_at"] == presence.iso(at(minutes=5))
    # Detected at 15 min: the window runs from there, not from the 5 min stamp.
    presence.sweep(pdb, now=at(minutes=16))
    assert row(pdb, thread)["present"] == 1
    presence.sweep(pdb, now=at(minutes=17))
    assert row(pdb, thread)["last_exited_at"] == presence.iso(at(minutes=5))

    other = new_thread(pdb, "idle-clamped")
    claim(pdb, other, at(minutes=30))
    release(pdb, at(minutes=31), reason="idle", idle_for=timedelta(hours=3))
    assert row(pdb, other)["exit_pending_at"] == presence.iso(at(minutes=30))


def test_hidden_page_waits_out_the_grace_before_the_record_exits(pdb):
    thread = new_thread(pdb, "hidden")
    claim(pdb, thread, at(0))
    release(pdb, at(60), reason="hidden")
    presence.sweep(pdb, now=at(89))
    assert row(pdb, thread)["exit_pending_at"] is None
    presence.sweep(pdb, now=at(91))
    assert row(pdb, thread)["exit_pending_at"] == presence.iso(at(60))

    back = new_thread(pdb, "hidden-back")
    claim(pdb, back, at(0))
    release(pdb, at(60), reason="hidden")
    claim(pdb, back, at(80))
    presence.sweep(pdb, now=at(minutes=10))
    assert row(pdb, back)["present"] == 1
    assert visits(pdb, back) == []


def test_finished_visit_row_is_written_with_the_exit(pdb):
    pdb.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('proj-p','P','t','t')"
    )
    thread = new_thread(pdb, "history", project_id="proj-p")
    claim(pdb, thread, at(0))
    release(pdb, at(minutes=4))
    presence.sweep(pdb, now=at(minutes=6))

    [visit] = visits(pdb, thread)
    assert dict(visit) | {"id": None} == {
        "id": None, "user_id": "presence-user", "surface": "chat",
        "thread_id": thread, "project_id": "proj-p", "object_id": None,
        "entered_at": presence.iso(at(0)),
        "exited_at": presence.iso(at(minutes=4)), "seconds": 240,
        "sessions": 1, "exit_reason": "navigated",
        # A claim that reports no focus counts as focused the whole visit.
        "focus_spans": f'[["{presence.iso(at(0))}","{presence.iso(at(minutes=4))}"]]',
    }


def test_turns_while_away_count_from_the_previous_exit(pdb):
    thread = new_thread(pdb, "turns")
    add_turn(pdb, thread, "before")
    claim(pdb, thread, at(0))
    add_turn(pdb, thread, "during")
    release(pdb, at(minutes=1))
    presence.sweep(pdb, now=at(minutes=4))
    add_turn(pdb, thread, "away-1")
    add_turn(pdb, thread, "away-2")

    claim(pdb, thread, at(minutes=61))

    entered = row(pdb, thread)
    assert entered["turns_while_away"] == 2
    assert entered["away_seconds"] == 3600
    assert entered["previous_entered_at"] == presence.iso(at(0))


def test_an_unsupported_surface_is_refused(pdb):
    thread = new_thread(pdb, "surface")
    try:
        presence.claim(pdb, user_id="presence-user", surface="documents",
                       thread_id=thread, session="s1", now=at(0))
    except ValueError:
        pass
    else:
        raise AssertionError("a non-chat surface was accepted in v1")
    assert row(pdb, thread) is None


def _pane_views(db, thread: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_pane_views WHERE thread_id=?", (thread,),
    ).fetchone()[0]


def _seen_in_pane(db, thread: str) -> None:
    import plans.pane_views as pane_views

    mark = "2026-09-24T12:00:00+00:00"
    pane_views.record(db, user_id="presence-user", thread_id=thread, seen_through=mark)
    assert _pane_views(db, thread) == 1


def test_thread_delete_removes_presence_and_pane_view_rows(pdb):
    from api.thread_deletion import delete_thread_rows
    from thread.transcript_order import destructive_boundary_version

    thread = new_thread(pdb, "deleted")
    claim(pdb, thread, at(0))
    release(pdb, at(60))
    presence.sweep(pdb, now=at(minutes=4))
    assert visits(pdb, thread)
    _seen_in_pane(pdb, thread)
    version = destructive_boundary_version(pdb, thread)

    assert delete_thread_rows(thread, "presence-user", {thread: version}) == "ok"
    assert row(pdb, thread) is None
    assert visits(pdb, thread) == []
    assert _pane_views(pdb, thread) == 0


def test_thread_purge_removes_presence_rows(pdb):
    from core import thread_lifecycle
    from infra.database import write_transaction

    thread = new_thread(pdb, "purged")
    claim(pdb, thread, at(0))
    release(pdb, at(60))
    presence.sweep(pdb, now=at(minutes=4))
    claim(pdb, thread, at(minutes=5))

    with write_transaction(pdb, scope="test_purge"):
        thread_lifecycle._apply_purge(pdb, thread, at(minutes=6))

    assert row(pdb, thread) is None
    assert visits(pdb, thread) == []
    # A claim still open on the purged thread never recreates its record, and
    # the next sweep after its release forgets it.
    release(pdb, at(minutes=7))
    presence.sweep(pdb, now=at(minutes=10))
    assert row(pdb, thread) is None
    assert visits(pdb, thread) == []
    assert presence.live_view("presence-user", thread) is None
