"""Shared setup for the user presence tests (issue #2004)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest

from infra import presence
from infra.database import get_db

USER = "presence-user"
T0 = datetime(2026, 9, 18, 20, 40, 0)  # naive UTC; 13:40 PDT


def at(seconds: float = 0, *, minutes: float = 0) -> datetime:
    """T0 plus an offset."""
    return T0 + timedelta(seconds=seconds, minutes=minutes)


@pytest.fixture
def pdb(isolated_db):
    """A connection to this test's isolated database, with presence memory cleared."""
    presence._visits.clear()
    presence._session_keys.clear()
    db = get_db()
    try:
        yield db
    finally:
        db.close()
        presence._visits.clear()
        presence._session_keys.clear()


@pytest.fixture
def pacific():
    """Render lines in America/Los_Angeles, the zone the design's examples use."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def new_thread(db, thread_id: str, *, parent: str | None = None,
               project_id: str | None = None) -> str:
    """Insert one thread owned by USER."""
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at,"
        "parent_thread_id,spawned_by,project_id) VALUES (?,?,?,?,?,?,?,?)",
        (thread_id, USER, "native/test", "t", "t", parent, parent, project_id),
    )
    db.commit()
    return thread_id


def add_reply(db, thread_id: str, when: datetime, content: str = "Done.") -> None:
    """Insert one delivered assistant row created at ``when``."""
    seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE thread_id=?",
        (thread_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
        "VALUES (?,?,?,?,?,?)",
        (f"{thread_id}-m{seq}", thread_id, "assistant", content,
         presence.iso(when), seq),
    )
    db.commit()


def add_turn(db, thread_id: str, turn_id: str) -> None:
    """Insert one completed Turn audit row."""
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
        "started_at,boot_epoch) VALUES (?,?,0,'native/test','completed','t','test')",
        (turn_id, thread_id),
    )
    db.commit()


def claim(db, thread_id: str, when: datetime, session: str = "s1") -> None:
    presence.claim(db, user_id=USER, surface="chat", thread_id=thread_id,
                   session=session, now=when)


def release(db, when: datetime, session: str = "s1", reason: str = "navigated",
            idle_for: timedelta | None = None) -> None:
    presence.release(db, session=session, surface="chat", reason=reason,
                     now=when, idle_for=idle_for)


def row(db, thread_id: str):
    return db.execute(
        "SELECT * FROM thread_presence WHERE user_id=? AND thread_id=?",
        (USER, thread_id),
    ).fetchone()


def visits(db, thread_id: str) -> list:
    return db.execute(
        "SELECT * FROM presence_visits WHERE thread_id=? ORDER BY id", (thread_id,),
    ).fetchall()
