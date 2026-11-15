"""Two-connection barriers exercise the real replacement transaction."""
from contextlib import contextmanager
from threading import Event, Thread

import pytest

from infra import presence, presence_records
from infra.database import get_db
from presence_helpers import USER, at, new_thread, pdb
from tools.post_update import _persist_update

__all__ = ["pdb"]


def save(thread, text="original", ids=()):
    return _persist_update(thread, text, None, None, "turn", ids, "updates" if ids else None)[0]


def claimant(thread, started, errors):
    db = get_db()
    started.set()
    try:
        presence.claim(db, user_id=USER, surface="chat", thread_id=thread,
                       session="real-race", now=at(10_000_000))
    except BaseException as error:
        errors.append(error)
    finally:
        db.close()


def test_real_presence_wins_refuses_without_writes(pdb):
    thread = new_thread(pdb, "real-presence-wins")
    old = save(thread)
    started, errors = Event(), []
    worker = Thread(target=claimant, args=(thread, started, errors), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and not errors
    with pytest.raises(ValueError, match="was seen"):
        save(thread, "replacement", (old,))
    assert pdb.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (thread,)).fetchone()[0] == 1
    assert pdb.execute("SELECT replaced_by FROM message_update_state WHERE message_id=?", (old,)).fetchone()[0] is None


def test_real_waiting_presence_refuses_without_deadlock(pdb, monkeypatch):
    import infra.message_update_seen as evidence

    thread = new_thread(pdb, "real-contention")
    old = save(thread)
    under_presence_lock, started, errors = Event(), Event(), []
    original_enter, original_snapshot = presence_records.enter, evidence.snapshot
    worker = Thread(target=claimant, args=(thread, started, errors), daemon=True)

    def announced_enter(*args, **kwargs):
        under_presence_lock.set()
        return original_enter(*args, **kwargs)

    @contextmanager
    def barrier_snapshot(*args):
        worker.start()  # Replacement already holds SQLite; claimant takes presence then waits for SQLite.
        assert under_presence_lock.wait(5)
        with original_snapshot(*args) as live:
            yield live

    monkeypatch.setattr(presence_records, "enter", announced_enter)
    monkeypatch.setattr(evidence, "snapshot", barrier_snapshot)
    try:
        with pytest.raises(ValueError, match="counts as seen"):
            save(thread, "replacement", (old,))
    finally:
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert pdb.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (thread,)).fetchone()[0] == 1


def test_real_replacement_commits_before_waiting_presence(pdb, monkeypatch):
    import infra.message_update_state as updates

    thread = new_thread(pdb, "real-replacement-wins")
    old = save(thread)
    started, errors = Event(), []
    worker = Thread(target=claimant, args=(thread, started, errors), daemon=True)
    original = updates.apply_replacement

    def barrier_apply(*args):
        original(*args)
        worker.start()  # The actual transaction retains the presence lock through commit.
        assert started.wait(5)

    monkeypatch.setattr(updates, "apply_replacement", barrier_apply)
    new = save(thread, "replacement", (old,))
    worker.join(5)
    assert not worker.is_alive() and not errors
    rows = pdb.execute("SELECT message_id,replaced_by FROM message_update_state WHERE thread_id=?", (thread,)).fetchall()
    assert {row[0]: row[1] for row in rows} == {old: new, new: None}
