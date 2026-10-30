"""Window focus on the presence record: spans written with the visit, never a second source of truth."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from infra import presence, presence_records
from tests.presence_helpers import USER, at, new_thread, pdb, release, row, visits

__all__ = ["pdb"]

SURFACE = "chat"


def _focus_claim(db, thread, when, *, session="s1", focused: bool | None = True) -> None:
    presence.claim(db, user_id=USER, surface=SURFACE, thread_id=thread, session=session,
                   now=when, focused=focused)


def _set_focus(db, when, focused: bool, session: str = "s1") -> None:
    presence.set_focus(db, session=session, surface=SURFACE, focused=focused, now=when)


def _spans(raw: str | None) -> list[tuple[datetime, datetime]] | None:
    return None if raw is None else presence_records.decode_spans(raw)


def _pairs(*pairs: tuple[datetime, datetime]) -> list[tuple[datetime, datetime]]:
    return list(pairs)


def _fail_focus_writes(monkeypatch) -> None:
    def failing(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(presence_records, "write_focus", failing)


def test_a_glance_away_closes_a_span_and_a_return_opens_the_next(pdb):
    thread = new_thread(pdb, "glance")
    _focus_claim(pdb, thread, at(0))
    assert row(pdb, thread)["focus_since"] == presence.iso(at(0))

    _set_focus(pdb, at(10), False)
    _set_focus(pdb, at(12), True)
    release(pdb, at(60))
    presence.sweep(pdb, now=at(minutes=4))

    [visit] = visits(pdb, thread)
    assert _spans(visit["focus_spans"]) == _pairs((at(0), at(10)), (at(12), at(60)))
    assert row(pdb, thread)["focus_spans"] is None and row(pdb, thread)["focus_since"] is None


def test_a_claim_reporting_no_focus_counts_as_focused(pdb):
    thread = new_thread(pdb, "unreported")
    _focus_claim(pdb, thread, at(0), focused=None)
    release(pdb, at(30))
    presence.sweep(pdb, now=at(minutes=4))

    [visit] = visits(pdb, thread)
    assert _spans(visit["focus_spans"]) == _pairs((at(0), at(30)))


def test_a_claim_that_starts_blurred_opens_no_span(pdb):
    thread = new_thread(pdb, "blurred")
    _focus_claim(pdb, thread, at(0), focused=False)
    assert row(pdb, thread)["focus_since"] is None
    _set_focus(pdb, at(20), True)
    release(pdb, at(50))
    presence.sweep(pdb, now=at(minutes=4))

    [visit] = visits(pdb, thread)
    assert _spans(visit["focus_spans"]) == _pairs((at(20), at(50)))


def test_a_visit_with_two_sessions_is_focused_while_either_is_and_exits_at_the_last_left(pdb):
    thread = new_thread(pdb, "two-sessions")
    _focus_claim(pdb, thread, at(0), session="a")
    _focus_claim(pdb, thread, at(0), session="b")

    release(pdb, at(40), session="a")  # b still shows the thread, so the visit stays open
    assert row(pdb, thread)["focus_since"] == presence.iso(at(0))
    _set_focus(pdb, at(50), False, session="b")  # no session is focused any more
    release(pdb, at(60), session="b", reason="hidden")
    presence.sweep(pdb, now=at(91))  # the hidden grace ends and the exit starts
    presence.sweep(pdb, now=at(minutes=4))  # the merge window passes

    [visit] = visits(pdb, thread)
    assert visit["exited_at"] == presence.iso(at(60))
    assert _spans(visit["focus_spans"]) == _pairs((at(0), at(50)))


def test_a_blurred_session_does_not_hide_the_focus_of_another(pdb):
    thread = new_thread(pdb, "mixed")
    _focus_claim(pdb, thread, at(0), session="a")
    _focus_claim(pdb, thread, at(0), session="b", focused=False)
    _set_focus(pdb, at(10), False, session="a")
    _set_focus(pdb, at(20), True, session="b")
    release(pdb, at(30), session="a")
    release(pdb, at(40), session="b")
    presence.sweep(pdb, now=at(minutes=4))

    [visit] = visits(pdb, thread)
    assert _spans(visit["focus_spans"]) == _pairs((at(0), at(10)), (at(20), at(40)))


def test_a_crash_counts_only_the_time_the_record_confirms(pdb):
    """Focus 10:00-10:10, again from 10:20, last confirmed 10:25, restart at 16:00: 15 minutes."""
    thread = new_thread(pdb, "crash")
    _focus_claim(pdb, thread, at(minutes=0))
    _set_focus(pdb, at(minutes=10), False)
    _set_focus(pdb, at(minutes=20), True)
    presence.sweep(pdb, now=at(minutes=25))  # the keep-alive confirms the visit at 10:25
    assert row(pdb, thread)["last_confirmed_at"] == presence.iso(at(minutes=25))

    presence.reconcile_startup(pdb, now=at(minutes=6 * 60 - 40))
    presence.sweep(pdb, now=at(minutes=6 * 60 - 30))

    [visit] = visits(pdb, thread)
    spans = _spans(visit["focus_spans"])
    assert spans == _pairs((at(minutes=0), at(minutes=10)), (at(minutes=20), at(minutes=25)))
    assert sum((end - start).total_seconds() for start, end in spans) == 15 * 60


def test_a_reconnect_after_restart_continues_the_visit_with_its_spans_closed(pdb):
    thread = new_thread(pdb, "restart-continue")
    _focus_claim(pdb, thread, at(0))
    presence.sweep(pdb, now=at(minutes=6))
    presence.reconcile_startup(pdb, now=at(minutes=30))
    assert _spans(row(pdb, thread)["focus_spans"]) == _pairs((at(0), at(minutes=6)))
    assert row(pdb, thread)["focus_since"] is None

    _focus_claim(pdb, thread, at(minutes=31))  # inside the merge window: the same visit
    assert _spans(row(pdb, thread)["focus_spans"]) == _pairs((at(0), at(minutes=6)))


def test_a_lost_focus_write_is_repaired_by_the_next_sweep(pdb, monkeypatch):
    thread = new_thread(pdb, "lost-write")
    _focus_claim(pdb, thread, at(0))
    real = presence_records.write_focus

    _fail_focus_writes(monkeypatch)
    _set_focus(pdb, at(10), False)  # the write fails; memory holds the span
    monkeypatch.setattr(presence_records, "write_focus", real)
    assert row(pdb, thread)["focus_since"] == presence.iso(at(0))  # the row is behind

    presence.sweep(pdb, now=at(20))
    assert _spans(row(pdb, thread)["focus_spans"]) == _pairs((at(0), at(10)))
    assert row(pdb, thread)["focus_since"] is None


def test_a_visit_finalized_with_a_dirty_row_records_the_spans_held_in_memory(pdb, monkeypatch):
    thread = new_thread(pdb, "lost-then-exit")
    _focus_claim(pdb, thread, at(0))
    real = presence_records.write_focus

    _fail_focus_writes(monkeypatch)
    _set_focus(pdb, at(10), False)
    _set_focus(pdb, at(20), True)
    release(pdb, at(30))  # the exit writes while focus writes still fail
    monkeypatch.setattr(presence_records, "write_focus", real)
    presence.sweep(pdb, now=at(minutes=4))

    [visit] = visits(pdb, thread)
    assert _spans(visit["focus_spans"]) == _pairs((at(0), at(10)), (at(20), at(30)))


def test_a_visit_that_predates_focus_tracking_stays_whole(pdb):
    """The upgrade straddle: a row opened before the columns existed keeps NULL spans to its exit."""
    thread = new_thread(pdb, "legacy")
    pdb.execute(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,last_confirmed_at,updated_at) "
        "VALUES (?,?,1,?,?,?)", (USER, thread, presence.iso(at(0)), presence.iso(at(0)), presence.iso(at(0))),
    )
    pdb.commit()

    _focus_claim(pdb, thread, at(10))  # continues the open, NULL-span visit
    _set_focus(pdb, at(20), False)
    _set_focus(pdb, at(30), True)
    assert row(pdb, thread)["focus_spans"] is None and row(pdb, thread)["focus_since"] is None

    release(pdb, at(60))
    presence.sweep(pdb, now=at(minutes=4))
    [visit] = visits(pdb, thread)
    assert visit["focus_spans"] is None


def test_a_legacy_visit_survives_a_restart_unchanged(pdb):
    thread = new_thread(pdb, "legacy-restart")
    pdb.execute(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,last_confirmed_at,updated_at) "
        "VALUES (?,?,1,?,?,?)", (USER, thread, presence.iso(at(0)), presence.iso(at(60)), presence.iso(at(0))),
    )
    pdb.commit()
    presence.reconcile_startup(pdb, now=at(minutes=30))
    assert row(pdb, thread)["focus_spans"] is None


def test_a_reused_row_starts_each_new_visit_with_fresh_focus(pdb):
    thread = new_thread(pdb, "reused")
    _focus_claim(pdb, thread, at(0))
    _set_focus(pdb, at(5), False)
    release(pdb, at(10))
    presence.sweep(pdb, now=at(minutes=4))
    assert row(pdb, thread)["present"] == 0

    _focus_claim(pdb, thread, at(minutes=10), focused=False)
    assert row(pdb, thread)["focus_spans"] == "[]"
    assert row(pdb, thread)["focus_since"] is None

    release(pdb, at(minutes=11))
    presence.sweep(pdb, now=at(minutes=20))
    _focus_claim(pdb, thread, at(minutes=30))
    assert row(pdb, thread)["focus_spans"] == "[]"
    assert row(pdb, thread)["focus_since"] == presence.iso(at(minutes=30))


def test_the_span_list_never_leaves_its_visit(pdb):
    """A span recorded before the entry is dropped and an open one ends at the exit."""
    thread = new_thread(pdb, "clipped")
    _focus_claim(pdb, thread, at(minutes=10))
    pdb.execute(
        "UPDATE thread_presence SET focus_spans=?, focus_since=?, exit_pending_at=?, exit_pending_since=?, "
        "exit_pending_reason='idle' WHERE thread_id=?",
        (presence_records.encode_spans([(at(minutes=1), at(minutes=5))]), presence.iso(at(minutes=11)),
         presence.iso(at(minutes=12)), presence.iso(at(minutes=12)), thread),
    )
    pdb.commit()
    presence._visits.clear()
    presence._session_keys.clear()
    presence.sweep(pdb, now=at(minutes=20))

    [visit] = visits(pdb, thread)
    assert _spans(visit["focus_spans"]) == _pairs((at(minutes=11), at(minutes=12)))


_EVENTS = {
    "blur and refocus": [(10, False), (20, True)],
    "blur and stay away": [(10, False)],
    "a burst of switches": [(second, bool(second // 5 % 2)) for second in range(5, 60, 5)],
    "refocus without a blur": [(10, True)],
}


@pytest.mark.parametrize("events", _EVENTS.values(), ids=list(_EVENTS))
def test_focus_never_changes_who_is_present_or_when_the_visit_ends(pdb, events):
    thread = new_thread(pdb, "unchanged")
    _focus_claim(pdb, thread, at(0))
    before = dict(row(pdb, thread))
    live_before = presence.live_view(USER, thread)

    for second, focused in events:
        _set_focus(pdb, at(second), focused)

    after = dict(row(pdb, thread))
    focus_state = {"focus_spans", "focus_since", "last_confirmed_at", "updated_at"}
    assert {k: v for k, v in after.items() if k not in focus_state} == {
        k: v for k, v in before.items() if k not in focus_state
    }
    assert presence.live_view(USER, thread) == live_before == ("here", None)
    assert visits(pdb, thread) == []


def test_focus_on_an_unclaimed_session_changes_nothing(pdb):
    thread = new_thread(pdb, "unclaimed")
    assert presence.set_focus(pdb, session="nobody", surface=SURFACE, focused=False, now=at(0)) == []
    assert row(pdb, thread) is None
    assert presence.is_claimed(session="nobody", surface=SURFACE, thread_id=thread) is False
    _focus_claim(pdb, thread, at(0))
    assert presence.is_claimed(session="s1", surface=SURFACE, thread_id=thread) is True
    assert presence.is_claimed(session="s1", surface=SURFACE, thread_id="other") is False
