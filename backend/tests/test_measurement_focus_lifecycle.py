"""Measurement focus intervals close with their presence claim, and never break it."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from infra import measurement_focus, presence, presence_time
from tests.presence_helpers import USER, new_thread, pdb

__all__ = ["pdb"]

T0 = datetime(2026, 10, 16, 12, 0)


def _blurred(pdb, thread: str) -> list[str | None]:
    return [row["blurred_at"] for row in pdb.execute(
        "SELECT blurred_at FROM measurement_focus_intervals WHERE thread_id=? ORDER BY id", (thread,),
    )]


def _focus(pdb, thread: str, session: str = "s1") -> None:
    presence.claim(pdb, user_id=USER, surface="chat", thread_id=thread,
                   session=session, now=T0, focused=True)
    assert _blurred(pdb, thread) == [None]


def test_a_disconnect_closes_the_open_interval_at_the_close(pdb):
    thread = new_thread(pdb, "focus-disconnect")
    _focus(pdb, thread)

    presence.release_session(pdb, session="s1", now=T0 + timedelta(minutes=3))

    assert _blurred(pdb, thread) == [presence_time.iso(T0 + timedelta(minutes=3))]


def test_navigating_away_closes_the_open_interval(pdb):
    thread = new_thread(pdb, "focus-navigate")
    _focus(pdb, thread)

    presence.release(pdb, session="s1", surface="chat", reason="navigated", now=T0 + timedelta(minutes=2))

    assert _blurred(pdb, thread) == [presence_time.iso(T0 + timedelta(minutes=2))]


def test_a_restart_closes_every_open_interval(pdb):
    thread = new_thread(pdb, "focus-restart")
    _focus(pdb, thread)

    presence.reconcile_startup(pdb, now=T0 + timedelta(minutes=5))

    assert _blurred(pdb, thread) == [presence_time.iso(T0 + timedelta(minutes=5))]


def test_deleting_the_thread_removes_its_intervals(pdb):
    thread = new_thread(pdb, "focus-delete")
    other = new_thread(pdb, "focus-keep")
    _focus(pdb, thread)
    _focus(pdb, other, session="s2")

    presence.delete_thread_presence(pdb, thread)

    assert _blurred(pdb, thread) == []
    assert _blurred(pdb, other) == [None]


@pytest.fixture
def failing_focus_writes(monkeypatch):
    """Make every Measurement focus write fail the way a locked database does."""
    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(measurement_focus, "write_transaction", refuse)


def test_a_failed_focus_write_never_strands_the_shared_claim(pdb, failing_focus_writes):
    thread = new_thread(pdb, "focus-write-fails")

    presence.claim(pdb, user_id=USER, surface="chat", thread_id=thread,
                   session="s1", now=T0, focused=True)
    assert presence.live_view(USER, thread) == ("here", None)
    presence.release_session(pdb, session="s1", now=T0 + timedelta(minutes=1))

    assert presence.live_view(USER, thread)[0] != "here"
    assert _blurred(pdb, thread) == []


def test_a_failed_focus_write_does_not_skip_the_shared_restart_reconcile(pdb, failing_focus_writes):
    thread = new_thread(pdb, "focus-reconcile-fails")
    presence.claim(pdb, user_id=USER, surface="chat", thread_id=thread,
                   session="s1", now=T0)
    pdb.commit()

    presence.reconcile_startup(pdb, now=T0 + timedelta(minutes=5))

    row = pdb.execute(
        "SELECT exit_pending_reason FROM thread_presence WHERE thread_id=?", (thread,),
    ).fetchone()
    assert row["exit_pending_reason"] == "restart"
