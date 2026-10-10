"""The update dot's server-side seen state (update cards r2, PR 4; AS7, AS10).

The dot reads only committed ``thread_presence`` columns, never the
in-memory claim: an update newer than the visit's exit lights it, being here
clears it, Mark read raises the boundary, Mark unread holds it on until a new
visit opens. Every committed presence change is nudged after commit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import infra.presence_records as presence_records
import ws as ws_endpoint
from api import thread_seen
from infra import presence, thread_updates
from infra.auth import require_user
from tests.presence_helpers import USER, at, claim, new_thread, pdb, release, row
from ws import presence_control

__all__ = ["pdb"]


def _update(db, thread_id: str, when: datetime, message_id: str) -> None:
    thread_updates.record_marked(
        db, thread_id=thread_id, message_id=message_id, node_id=None,
        created_at=presence.iso(when),
    )
    db.commit()


def _seen(db, *thread_ids: str) -> dict[str, dict]:
    ids = list(thread_ids)
    return thread_updates.seen_by_thread(
        db, USER, thread_updates.latest_by_thread(db, ids), ids,
    )


def _dot(db, thread_id: str) -> bool | None:
    state = _seen(db, thread_id).get(thread_id)
    return None if state is None else state["has_update"]


def _mark(db, thread_id: str, *, unread: bool, when: datetime) -> bool:
    return presence.mark_seen(db, user_id=USER, thread_id=thread_id, unread=unread, now=when)


def test_a_never_opened_thread_has_seen_nothing(pdb):
    thread = new_thread(pdb, "never")
    assert _seen(pdb, thread) == {}

    _update(pdb, thread, at(5), "m1")

    assert _seen(pdb, thread) == {thread: {"has_update": True, "opened": False}}


def test_being_here_sees_everything_and_leaving_bounds_it(pdb):
    thread = new_thread(pdb, "here")
    claim(pdb, thread, at(0))
    _update(pdb, thread, at(10), "during")

    assert _seen(pdb, thread) == {thread: {"has_update": False, "opened": True}}

    release(pdb, at(20))  # exit pending, stamped at 20 s
    assert row(pdb, thread)["present"] == 1
    assert _dot(pdb, thread) is False

    _update(pdb, thread, at(30), "after")
    assert _dot(pdb, thread) is True

    presence.sweep(pdb, now=at(20) + presence.MERGE_WINDOW)  # finalized
    assert row(pdb, thread)["present"] == 0
    assert _dot(pdb, thread) is True


def test_an_update_inside_the_hidden_grace_lights_the_dot_once_the_exit_commits(pdb):
    thread = new_thread(pdb, "hidden")
    claim(pdb, thread, at(0))
    release(pdb, at(10), reason="hidden")
    _update(pdb, thread, at(15), "in-grace")

    # The record has not started its exit yet: the user still counts as here.
    assert _dot(pdb, thread) is False
    changed = presence.sweep(pdb, now=at(40))

    assert changed == [(USER, thread)]
    assert row(pdb, thread)["exit_pending_at"] == presence.iso(at(10))
    assert _dot(pdb, thread) is True


def test_dot_and_divider_use_the_same_committed_seen_boundary(pdb):
    thread = new_thread(pdb, "shared-boundary")
    claim(pdb, thread, at(0))
    release(pdb, at(10))
    _update(pdb, thread, at(20), "after-exit")

    committed = row(pdb, thread)
    assert presence.seen_boundary(committed) == presence.iso(at(10))
    assert _dot(pdb, thread) is True
    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(10)), "first_seq": None,
    }

    assert _mark(pdb, thread, unread=False, when=at(30))
    assert presence.seen_boundary(row(pdb, thread)) == presence.iso(at(30))
    assert _dot(pdb, thread) is False
    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(30)), "first_seq": None,
    }


def test_a_failed_entry_write_leaves_the_dot_on_and_nudges_nothing(pdb, monkeypatch):
    thread = new_thread(pdb, "failed-entry")
    _update(pdb, thread, at(0), "m1")
    nudges: list = []
    monkeypatch.setattr(thread_updates, "notify_changed", lambda *args: nudges.append(args))

    def _broken_enter(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(presence_records, "enter", _broken_enter)
    with pytest.raises(RuntimeError):
        asyncio.run(presence_control._apply(
            presence.claim, user_id=USER, surface="chat", thread_id=thread, session="s1",
        ))

    assert presence.live_view(USER, thread) == ("here", None)  # memory claims it
    assert row(pdb, thread) is None
    assert _dot(pdb, thread) is True
    assert nudges == []


def test_committed_entry_and_exit_nudge_the_user_after_commit(pdb, monkeypatch):
    thread = new_thread(pdb, "nudged")
    committed: list = []

    def _notify(user_id, thread_ids):
        committed.append((user_id, thread_ids, row(pdb, thread)["present"],
                          row(pdb, thread)["exit_pending_at"]))

    monkeypatch.setattr(thread_updates, "notify_changed", _notify)
    socket = object()

    async def _scenario() -> None:
        frame = {"type": "presence", "surface": "chat", "thread_id": thread}
        await presence_control.handle_presence(
            socket, {"id": USER}, {**frame, "state": "foreground"},
            owned_thread_fn=ws_endpoint._owned_thread,
        )
        await presence_control.handle_presence(
            socket, {"id": USER}, {**frame, "state": "background"},
            owned_thread_fn=ws_endpoint._owned_thread,
        )

    asyncio.run(_scenario())

    assert [(user, ids, present) for user, ids, present, _ in committed] == [
        (USER, [thread], 1), (USER, [thread], 1),
    ]
    assert committed[0][3] is None and committed[1][3] is not None


def test_navigating_away_nudges_the_thread_left_and_the_thread_entered(pdb):
    first, second = new_thread(pdb, "first"), new_thread(pdb, "second")
    claim(pdb, first, at(0))

    changed = presence.claim(pdb, user_id=USER, surface="chat", thread_id=second,
                             session="s1", now=at(10))

    assert changed == [(USER, first), (USER, second)]


def test_mark_read_raises_the_boundary_and_mark_unread_holds_the_dot(pdb):
    thread = new_thread(pdb, "marks")
    claim(pdb, thread, at(0))
    release(pdb, at(10))
    _update(pdb, thread, at(20), "m1")
    assert _dot(pdb, thread) is True

    assert _mark(pdb, thread, unread=False, when=at(30))
    assert row(pdb, thread)["marked_read_at"] == presence.iso(at(30))
    assert _dot(pdb, thread) is False

    _update(pdb, thread, at(40), "m2")
    assert _dot(pdb, thread) is True
    assert _mark(pdb, thread, unread=False, when=at(50))
    assert _dot(pdb, thread) is False

    assert _mark(pdb, thread, unread=True, when=at(60))
    assert row(pdb, thread)["marked_unread_at"] == presence.iso(at(60))
    assert _dot(pdb, thread) is True
    # Mark read clears a Mark unread.
    assert _mark(pdb, thread, unread=False, when=at(70))
    assert row(pdb, thread)["marked_unread_at"] is None
    assert _dot(pdb, thread) is False


def test_every_committed_entry_is_a_new_seen_boundary(pdb):
    thread = new_thread(pdb, "unread-visit")
    claim(pdb, thread, at(0))
    assert _mark(pdb, thread, unread=True, when=at(5))
    assert _dot(pdb, thread) is True

    # Read A, leave, Mark unread while away, return inside the merge window:
    # the merged re-entry is still an entry, so the user has now seen A.
    release(pdb, at(10))
    assert _mark(pdb, thread, unread=True, when=at(15))
    assert _dot(pdb, thread) is True
    claim(pdb, thread, at(20))
    merged = row(pdb, thread)
    assert merged["exit_pending_at"] is None  # the same visit continued
    assert (merged["marked_unread_at"], merged["marked_read_at"]) == (None, None)
    assert _dot(pdb, thread) is False

    assert _mark(pdb, thread, unread=True, when=at(25))
    release(pdb, at(30))
    presence.sweep(pdb, now=at(30) + presence.MERGE_WINDOW)
    claim(pdb, thread, at(minutes=10))  # a new visit opens
    reopened = row(pdb, thread)
    assert (reopened["marked_unread_at"], reopened["marked_read_at"]) == (None, None)
    assert _dot(pdb, thread) is False


def test_a_mark_never_fabricates_a_visit(pdb):
    thread = new_thread(pdb, "unopened")
    _update(pdb, thread, at(0), "m1")

    assert _mark(pdb, thread, unread=False, when=at(5)) is False
    assert _mark(pdb, thread, unread=True, when=at(5)) is False
    assert row(pdb, thread) is None
    assert _seen(pdb, thread) == {thread: {"has_update": True, "opened": False}}


def test_the_seen_read_is_one_primary_key_probe_per_thread(pdb):
    ids = [new_thread(pdb, f"plan-{index}") for index in range(3)]
    marks = ",".join("?" * len(ids))
    plan = " ".join(
        str(step[-1]) for step in pdb.execute(
            f"""EXPLAIN QUERY PLAN SELECT thread_id,present,exit_pending_at,last_exited_at,
                       marked_read_at,marked_unread_at FROM thread_presence
                 WHERE user_id=? AND thread_id IN ({marks})""",
            (USER, *ids),
        )
    )
    assert "USING PRIMARY KEY" in plan or "sqlite_autoindex_thread_presence" in plan
    assert "SCAN thread_presence" not in plan
    with pytest.raises(ValueError, match="at most 200"):
        thread_updates.seen_by_thread(pdb, USER, {}, [f"t{index}" for index in range(201)])


@pytest.fixture
def client(pdb, monkeypatch):
    app = FastAPI()
    app.include_router(thread_seen.router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: {"id": USER}
    nudges: list = []
    monkeypatch.setattr(thread_updates, "notify_changed", lambda *args: nudges.append(args))
    with TestClient(app) as test_client:
        test_client.nudges = nudges
        yield test_client


def test_the_mark_endpoint_writes_seen_state_and_nudges_after_commit(pdb, client):
    thread = new_thread(pdb, "api")
    claim(pdb, thread, at(0))
    release(pdb, at(10))

    response = client.post(f"/api/threads/{thread}/seen", json={"unread": True})

    assert response.status_code == 200
    assert row(pdb, thread)["marked_unread_at"] is not None
    assert client.nudges == [(USER, [thread])]


def test_the_mark_endpoint_refuses_unopened_foreign_and_purged_threads(pdb, client):
    unopened = new_thread(pdb, "api-unopened")
    pdb.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('foreign','someone-else','native/test','t','t')"
    )
    purged = new_thread(pdb, "api-purged")
    claim(pdb, purged, at(0))
    pdb.execute("UPDATE threads SET purged_at='t' WHERE id=?", (purged,))
    pdb.commit()

    assert client.post(f"/api/threads/{unopened}/seen", json={"unread": False}).status_code == 409
    assert client.post("/api/threads/foreign/seen", json={"unread": False}).status_code == 404
    assert client.post(f"/api/threads/{purged}/seen", json={"unread": True}).status_code == 404
    assert client.post(f"/api/threads/{unopened}/seen", json={}).status_code == 422
    assert row(pdb, unopened) is None
    assert client.nudges == []


def _events(monkeypatch) -> list[dict]:
    from infra import product_events

    events: list[dict] = []
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda category, action, **kwargs: events.append({"event": f"{category}.{action}", **kwargs}),
    )
    return events


def test_the_mark_endpoint_reports_one_event_per_read_and_unread(pdb, client, monkeypatch):
    events = _events(monkeypatch)
    thread = new_thread(pdb, "api-events")
    claim(pdb, thread, at(0))
    release(pdb, at(10))

    assert client.post(f"/api/threads/{thread}/seen", json={"unread": True}).status_code == 200
    assert client.post(f"/api/threads/{thread}/seen", json={"unread": False}).status_code == 200

    assert events == [
        {"event": "thread.seen_mark", "thread_id": thread, "user_id": USER,
         "properties": {"mark": "unread", "outcome": "succeeded"}},
        {"event": "thread.seen_mark", "thread_id": thread, "user_id": USER,
         "properties": {"mark": "read", "outcome": "succeeded"}},
    ]


def test_the_mark_endpoint_reports_each_refusal_once_with_a_bounded_reason(pdb, client, monkeypatch):
    events = _events(monkeypatch)
    unopened = new_thread(pdb, "api-events-unopened")

    assert client.post(f"/api/threads/{unopened}/seen", json={"unread": False}).status_code == 409
    assert client.post("/api/threads/absent/seen", json={"unread": True}).status_code == 404

    assert events == [
        {"event": "thread.seen_mark", "thread_id": unopened, "user_id": USER,
         "properties": {"mark": "read", "outcome": "refused", "reason": "never_opened"}},
        {"event": "thread.seen_mark", "thread_id": "absent", "user_id": USER,
         "properties": {"mark": "unread", "outcome": "refused", "reason": "missing"}},
    ]


def test_the_mark_endpoint_reports_an_internal_failure_once_without_its_text(pdb, client, monkeypatch):
    events = _events(monkeypatch)
    thread = new_thread(pdb, "api-events-failed")
    claim(pdb, thread, at(0))

    def explode(*args, **kwargs):
        raise RuntimeError("secret database detail")

    monkeypatch.setattr(presence, "mark_seen", explode)
    with pytest.raises(RuntimeError):
        client.post(f"/api/threads/{thread}/seen", json={"unread": True})

    assert events == [
        {"event": "thread.seen_mark", "thread_id": thread, "user_id": USER,
         "properties": {"mark": "unread", "outcome": "failed", "reason": "internal_error"}},
    ]
    assert "secret" not in repr(events)
    assert client.nudges == []
