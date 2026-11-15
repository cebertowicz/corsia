"""Two-connection admission probe, before the replacement implementation."""
from contextlib import ExitStack
from threading import Event, Thread

import pytest

from infra import presence, presence_records
from infra.database import get_db, write_transaction
from presence_helpers import USER, at, new_thread, pdb

__all__ = ["pdb"]


def _prototype_replace(db, thread):
    """Exercise the proposed DB-to-presence nonblocking order, with a real write."""
    with ExitStack() as stack:
        with write_transaction(db, scope="replace_probe"):
            if not presence._LOCK.acquire(blocking=False):
                raise ValueError("unknown counts as seen")
            stack.callback(presence._LOCK.release)
            live = presence._live_view_locked(USER, thread)
            if live and live[0] == "here":
                raise ValueError("presence live now: seen")
            db.execute("UPDATE threads SET title='replacement' WHERE id=?", (thread,))


def _claim(thread, entered, errors):
    connection = get_db()
    try:
        presence.claim(connection, user_id=USER, surface="chat", thread_id=thread,
                       session="probe", now=at(10))
        entered.set()
    except BaseException as error:
        errors.append(error)
    finally:
        connection.close()


def test_presence_wins_refuses_with_two_connections(pdb):
    thread = new_thread(pdb, "probe-presence-wins")
    entered, errors = Event(), []
    worker = Thread(target=_claim, args=(thread, entered, errors), daemon=True)
    worker.start()
    assert entered.wait(5), errors
    worker.join(5)
    assert not worker.is_alive() and not errors
    with pytest.raises(ValueError, match="presence live now: seen"):
        _prototype_replace(pdb, thread)
    assert pdb.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0] != "replacement"


def test_presence_waiting_for_db_cannot_deadlock(pdb, monkeypatch):
    thread = new_thread(pdb, "probe-contended")
    under_presence_lock, finished, errors = Event(), Event(), []
    original_enter = presence_records.enter

    def announced_enter(*args, **kwargs):
        under_presence_lock.set()
        return original_enter(*args, **kwargs)

    monkeypatch.setattr(presence_records, "enter", announced_enter)
    worker = Thread(target=_claim, args=(thread, finished, errors), daemon=True)
    try:
        with write_transaction(pdb, scope="probe_holds_db"):
            worker.start()
            assert under_presence_lock.wait(5)
            with pytest.raises(ValueError, match="unknown counts as seen"):
                _prototype_replace(pdb, thread)
    finally:
        worker.join(5)
    assert not worker.is_alive(), "DB/presence lock inversion deadlocked"
    assert finished.is_set() and not errors
    assert pdb.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0] != "replacement"


def test_replacement_wins_before_presence(pdb):
    thread = new_thread(pdb, "probe-replace-wins")
    _prototype_replace(pdb, thread)
    entered, errors = Event(), []
    worker = Thread(target=_claim, args=(thread, entered, errors), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and entered.is_set() and not errors
    assert pdb.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0] == "replacement"
