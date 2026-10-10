"""Transcript update cards and the "Since your last visit" divider (PR 5; AS8, AS9, AS17).

Cards page by ``(after_seq, id)`` and say whether their plan item still
exists. The divider reads the committed presence row before this visit's own
entry commits, from the same boundary the update dot uses.
"""

from __future__ import annotations

from datetime import datetime
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import thread_update_cards
from infra import presence, thread_updates, visit_divider
from infra.auth import require_user
from plans import service
from tests.presence_helpers import USER, add_reply, at, claim, new_thread, pdb, release
from tests.plan_writer_authorizer import raw_plan_seed

__all__ = ["pdb"]


def _node(db, thread_id: str, title: str = "Ship the export") -> str:
    plan = service.ensure_plan("thread", thread_id, db=db)
    node_id = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="done", db=db,
    ).id
    db.commit()
    return node_id


def _card(db, thread_id: str, event_id: str, node_id: str, when: datetime, kind="done") -> None:
    thread_updates.record_plan_update(
        db, thread_id=thread_id, kind=kind, event_id=event_id, node_id=node_id,
        created_at=presence.iso(when),
    )
    db.commit()


def _left(db, thread_id: str, entered: datetime, exited: datetime) -> None:
    """One finished visit: entered, then left and finalized."""
    claim(db, thread_id, entered)
    release(db, exited)
    presence.sweep(db, now=exited + presence.MERGE_WINDOW)


def test_newest_cards_keep_first_page_cards_in_transcript_order(pdb):
    thread = new_thread(pdb, "newest-order")
    node_id = _node(pdb, thread)
    for index in range(5):
        _card(pdb, thread, f"e{index}", node_id, at(index))  # all at after_seq 0

    cards, has_more = thread_updates.read_newest_cards(pdb, thread, min_seq=0)

    assert has_more is False
    assert [card["id"] for card in cards] == sorted(card["id"] for card in cards)


def test_a_card_says_whether_its_plan_item_still_exists_and_keeps_its_title(pdb):
    thread = new_thread(pdb, "exists")
    kept, archived = _node(pdb, thread, "Kept"), _node(pdb, thread, "Gone")
    _card(pdb, thread, "e1", kept, at(1))
    _card(pdb, thread, "e2", archived, at(2))
    with raw_plan_seed(pdb):
        pdb.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (archived,))
    pdb.commit()

    cards, has_more = thread_updates.read_newest_cards(pdb, thread, min_seq=0)

    assert has_more is False
    assert [(card["title"], card["node_exists"]) for card in cards] == [
        ("Kept", True), ("Gone", False),
    ]


def test_a_page_of_cards_is_one_statement_however_many_cards_it_holds(pdb):
    thread = new_thread(pdb, "one-read")
    for index in range(5):
        _card(pdb, thread, f"e{index}", _node(pdb, thread, f"Item {index}"), at(index))
    statements: list[str] = []
    pdb.set_trace_callback(statements.append)
    try:
        cards, has_more = thread_updates.read_newest_cards(pdb, thread, min_seq=0)
    finally:
        pdb.set_trace_callback(None)

    assert has_more is False
    assert len(cards) == 5 and all(card["node_exists"] for card in cards)
    assert len(statements) == 1


def test_first_visit_has_no_divider_and_an_open_visit_keeps_its_pre_claim_boundary(pdb):
    thread = new_thread(pdb, "first")
    assert thread_updates.visit_divider(pdb, USER, thread) is None

    _left(pdb, thread, at(0), at(10))
    claim(pdb, thread, at(20))
    presence.mark_seen(pdb, user_id=USER, thread_id=thread, unread=False, now=at(22))
    pdb.commit()  # e.g. from another tab while this one holds the visit
    add_reply(pdb, thread, at(25))

    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(10)), "first_seq": None,
    }


def test_the_divider_sits_before_the_first_message_after_the_last_visit(pdb):
    thread = new_thread(pdb, "back")
    add_reply(pdb, thread, at(0))
    _left(pdb, thread, at(1), at(10))
    add_reply(pdb, thread, at(20))
    add_reply(pdb, thread, at(30))

    divider = thread_updates.visit_divider(pdb, USER, thread)

    assert divider == {"since": presence.iso(at(10)), "first_seq": 2}


def test_a_pending_exit_already_bounds_the_divider(pdb):
    thread = new_thread(pdb, "pending")
    claim(pdb, thread, at(0))
    release(pdb, at(10))  # present, exit pending
    add_reply(pdb, thread, at(20))

    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(10)), "first_seq": 1,
    }


def test_divider_is_the_same_whether_get_or_presence_claim_arrives_first(pdb, client):
    get_first = new_thread(pdb, "get-first")
    _left(pdb, get_first, at(0), at(10))
    add_reply(pdb, get_first, at(20))
    expected = {"since": presence.iso(at(10)), "first_seq": 1}

    assert client.get(f"/api/threads/{get_first}/updates", params={"divider": "true"}).json()["divider"] == expected
    claim(pdb, get_first, at(30))
    assert client.get(f"/api/threads/{get_first}/updates", params={"divider": "true"}).json()["divider"] == expected

    claim_first = new_thread(pdb, "claim-first")
    _left(pdb, claim_first, at(0), at(10))
    add_reply(pdb, claim_first, at(20))
    claim(pdb, claim_first, at(30))

    assert client.get(f"/api/threads/{claim_first}/updates", params={"divider": "true"}).json()["divider"] == expected


def test_mark_read_raises_the_divider_like_it_raises_the_dot(pdb):
    thread = new_thread(pdb, "marked")
    _left(pdb, thread, at(0), at(10))
    add_reply(pdb, thread, at(20))
    add_reply(pdb, thread, at(40))
    presence.mark_seen(pdb, user_id=USER, thread_id=thread, unread=False, now=at(30))
    pdb.commit()

    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(30)), "first_seq": 2,
    }


def test_only_new_cards_give_a_divider_with_no_message_position(pdb):
    thread = new_thread(pdb, "cards-only")
    add_reply(pdb, thread, at(0))
    _left(pdb, thread, at(1), at(10))
    _card(pdb, thread, "e1", _node(pdb, thread), at(20))

    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(10)), "first_seq": None,
    }


def test_divider_finds_the_first_new_message_after_many_old_messages(pdb):
    thread = new_thread(pdb, "many-old")
    _left(pdb, thread, at(0), at(10))
    for index in range(20):
        add_reply(pdb, thread, at(index - 20))
    add_reply(pdb, thread, at(20))
    add_reply(pdb, thread, at(30))

    assert thread_updates.visit_divider(pdb, USER, thread)["first_seq"] == 21


def test_message_times_in_other_iso_shapes_order_against_the_boundary(pdb):
    thread = new_thread(pdb, "shapes")
    _left(pdb, thread, at(0), at(10))
    for seq, stamp in ((1, "2026-09-18T20:40:05Z"), (2, "2026-09-18 20:40:20+00:00")):
        pdb.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq)"
            " VALUES (?,?,'assistant','x',?,?)",
            (f"{thread}-m{seq}", thread, stamp, seq),
        )
    pdb.commit()

    assert thread_updates.visit_divider(pdb, USER, thread)["first_seq"] == 2


def test_a_null_message_time_stops_the_divider_scan(pdb):
    thread = new_thread(pdb, "null-time")
    _left(pdb, thread, at(0), at(10))
    row = pdb.execute(
        "SELECT present,exit_pending_at,last_exited_at,marked_read_at FROM thread_presence"
        " WHERE user_id=? AND thread_id=?",
        (USER, thread),
    ).fetchone()

    class HistoricalRows:
        def execute(self, query, params):
            assert "ORDER BY seq DESC LIMIT ?" in query
            return [
                {"seq": 4, "created_at": presence.iso(at(40))},
                {"seq": 3, "created_at": None},
                {"seq": 2, "created_at": presence.iso(at(20))},
            ]

    divider = visit_divider.divider_from_row(HistoricalRows(), row, thread)

    assert divider["first_seq"] == 4


def test_the_divider_returns_the_oldest_scanned_message_at_its_cap(pdb, monkeypatch):
    thread = new_thread(pdb, "capped")
    _left(pdb, thread, at(0), at(10))
    add_reply(pdb, thread, at(0))
    for seconds in (20, 30, 40):
        add_reply(pdb, thread, at(seconds))
    monkeypatch.setattr(visit_divider, "DIVIDER_SCAN_LIMIT", 2)

    divider = thread_updates.visit_divider(pdb, USER, thread)

    assert divider["first_seq"] == 3
    assert divider["truncated"] is True


def test_a_release_is_not_delayed_by_a_divider_scan(pdb, monkeypatch):
    thread = new_thread(pdb, "lock-free-divider")
    releasing_thread = new_thread(pdb, "concurrent-release")
    claim(pdb, releasing_thread, at(-1), session="releasing")
    scanning, release_finished, allow_scan = threading.Event(), threading.Event(), threading.Event()

    def blocked_divider(db, row, thread_id):
        scanning.set()
        assert allow_scan.wait(timeout=1)
        return None

    monkeypatch.setattr(presence, "divider_from_row", blocked_divider)
    claiming = threading.Thread(
        target=presence.claim,
        kwargs={
            "db": pdb, "user_id": USER, "surface": "chat", "thread_id": thread,
            "session": "claiming", "now": at(0),
        },
    )
    releasing = threading.Thread(
        target=lambda: (
            presence.release(pdb, session="releasing", surface="chat", reason="navigated", now=at(1)),
            release_finished.set(),
        ),
    )

    claiming.start()
    try:
        assert scanning.wait(timeout=1)
        releasing.start()
        assert release_finished.wait(timeout=1)
    finally:
        allow_scan.set()
        claiming.join(timeout=1)
        releasing.join(timeout=1)

    assert not claiming.is_alive()
    assert not releasing.is_alive()


def test_a_visit_the_sweep_ends_during_a_claim_still_gets_its_divider(pdb, monkeypatch):
    thread = new_thread(pdb, "swept-mid-claim")
    claim(pdb, thread, at(0))
    release(pdb, at(10))  # exit pending: the next claim continues this visit and needs no divider
    add_reply(pdb, thread, at(20))
    lock, sections = presence._LOCK, []

    class SweepAfterFirstSection:
        def __enter__(self):
            return lock.__enter__()

        def __exit__(self, *exc):
            result = lock.__exit__(*exc)
            sections.append(exc)
            if len(sections) == 1:
                presence.sweep(pdb, now=at(10) + presence.MERGE_WINDOW)
            return result

    monkeypatch.setattr(presence, "_LOCK", SweepAfterFirstSection())
    claim(pdb, thread, at(10) + presence.MERGE_WINDOW)
    monkeypatch.setattr(presence, "_LOCK", lock)

    assert thread_updates.visit_divider(pdb, USER, thread) == {
        "since": presence.iso(at(10)), "first_seq": 1,
    }


@pytest.fixture
def client(pdb):
    app = FastAPI()
    app.include_router(thread_update_cards.router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: {"id": USER}
    with TestClient(app) as test_client:
        yield test_client


def test_the_endpoint_reads_cards_and_the_divider_only_when_asked(pdb, client):
    thread = new_thread(pdb, "api")
    add_reply(pdb, thread, at(0))
    _left(pdb, thread, at(1), at(10))
    _card(pdb, thread, "e1", _node(pdb, thread), at(20))

    plain = client.get(f"/api/threads/{thread}/updates")
    opened = client.get(f"/api/threads/{thread}/updates", params={"divider": "true"})

    assert plain.status_code == 200
    body = plain.json()
    assert [card["title"] for card in body["cards"]] == ["Ship the export"]
    assert body["has_more"] is False and body["divider"] is None
    assert opened.json()["divider"] == {"since": presence.iso(at(10)), "first_seq": None}


def test_the_endpoint_keeps_the_newest_thousand_cards_in_transcript_order(pdb, client):
    exact, overflow = new_thread(pdb, "exact-window"), new_thread(pdb, "overflow-window")
    for thread, count in ((exact, thread_updates.CARD_WINDOW_LIMIT), (overflow, thread_updates.CARD_WINDOW_LIMIT + 1)):
        node_id = _node(pdb, thread)
        for index in range(count):
            thread_updates.record_plan_update(
                pdb, thread_id=thread, kind="done", event_id=f"{thread}-{index}", node_id=node_id,
                created_at=presence.iso(at(index)),
            )
        pdb.commit()

    exact_ids = [row[0] for row in pdb.execute(
        "SELECT id FROM thread_updates WHERE thread_id=? ORDER BY after_seq,id", (exact,),
    )]
    overflow_ids = [row[0] for row in pdb.execute(
        "SELECT id FROM thread_updates WHERE thread_id=? ORDER BY after_seq,id", (overflow,),
    )]
    exact_body = client.get(f"/api/threads/{exact}/updates").json()
    overflow_body = client.get(f"/api/threads/{overflow}/updates").json()

    assert exact_body["has_more"] is False
    assert [card["id"] for card in exact_body["cards"]] == exact_ids
    assert overflow_body["has_more"] is True
    assert [card["id"] for card in overflow_body["cards"]] == overflow_ids[1:]


def test_the_endpoint_refuses_foreign_and_purged_threads(pdb, client):
    pdb.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('foreign','someone-else','native/test','t','t')"
    )
    purged = new_thread(pdb, "api-purged")
    pdb.execute("UPDATE threads SET purged_at='t' WHERE id=?", (purged,))
    pdb.commit()

    assert client.get("/api/threads/foreign/updates").status_code == 404
    assert client.get(f"/api/threads/{purged}/updates").status_code == 404
    assert client.get("/api/threads/foreign/updates", params={"from_seq": -1}).status_code == 422
