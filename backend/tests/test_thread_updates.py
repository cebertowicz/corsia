"""The thread update index: writers, bounded reads and deletion (update cards r2)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from infra import thread_updates
from infra.database import initialize_schema
from plans import service

_AT = "2026-09-29T10:00:00"


@pytest.fixture
def db():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        initialize_schema(connection)
        for thread_id, spawned_by in (("owner", None), ("worker", "owner"), ("other", None)):
            connection.execute(
                "INSERT INTO threads (id,user_id,spawned_by,title,created_at,updated_at)"
                " VALUES (?, 'user', ?, 'T', 't', 't')",
                (thread_id, spawned_by),
            )
        connection.commit()
        yield connection


def _node(db, thread_id: str = "owner", title: str = "Ship the export") -> str:
    plan = service.ensure_plan("thread", thread_id, db=db)
    return service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="done", db=db,
    ).id


def _message(db, thread_id: str, seq: int) -> str:
    message_id = f"{thread_id}-m{seq}"
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq)"
        " VALUES (?,?,'user','hi',?,?)",
        (message_id, thread_id, _AT, seq),
    )
    return message_id


def _plan_update(db, event_id: str, node_id: str, *, kind="done", at=_AT, thread_id="owner"):
    return thread_updates.record_plan_update(
        db, thread_id=thread_id, kind=kind, event_id=event_id, node_id=node_id, created_at=at,
    )


def _newest_reference(db, thread_id: str) -> dict | None:
    """The per-thread read latest_by_thread must keep matching, one seek each."""
    row = db.execute(
        f"SELECT {thread_updates._COLUMNS} FROM thread_updates WHERE thread_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if row is None:
        return None
    update = thread_updates._public(row)
    update["summary"] = None
    return update


class _StatementCounter:
    """A connection that records the statements a read issues through it."""

    def __init__(self, db):
        self._db = db
        self.statements: list[str] = []

    def execute(self, sql, parameters=()):
        self.statements.append(" ".join(sql.split()))
        return self._db.execute(sql, parameters)


def _rows(db) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT * FROM thread_updates ORDER BY id")]


def test_every_source_timestamp_is_stored_in_one_ordering_shape(db):
    node_id = _node(db)
    _plan_update(db, "event-offset", node_id, at="2026-09-29T10:00:01+00:00")
    thread_updates.record_marked(
        db, thread_id="owner", message_id="m-second", node_id=None, created_at="2026-09-29T10:00:02",
    )
    thread_updates.record_marked(
        db, thread_id="owner", message_id="m-micro", node_id=None,
        created_at="2026-09-29T03:00:03.250000-07:00",
    )

    assert [row["created_at"] for row in _rows(db)] == [
        "2026-09-29T10:00:01.000000",
        "2026-09-29T10:00:02.000000",
        "2026-09-29T10:00:03.250000",
    ]


def test_a_replayed_source_is_a_no_op_but_a_contract_violation_raises(db):
    node_id = _node(db)
    assert _plan_update(db, "event-1", node_id) is True
    assert _plan_update(db, "event-1", node_id, kind="failed") is False
    assert [(row["kind"], row["source_id"]) for row in _rows(db)] == [("done", "event-1")]

    violations = (
        {"kind": "done", "source_kind": "message", "node_id": node_id, "title": "T"},
        {"kind": "marked", "source_kind": "plan_event", "node_id": None, "title": None},
        {"kind": "marked", "source_kind": "message", "node_id": None, "title": "T"},
        {"kind": "done", "source_kind": "plan_event", "node_id": node_id, "title": "  "},
        {"kind": "done", "source_kind": "plan_event", "node_id": None, "title": "T"},
        {"kind": "reply_end", "source_kind": "message", "node_id": None, "title": None},
        {"kind": "thread_end", "source_kind": "message", "node_id": None, "title": None},
    )
    for index, fields in enumerate(violations):
        with pytest.raises(sqlite3.IntegrityError):
            thread_updates._record(
                db, thread_id="owner", source_id=f"bad-{index}", created_at=_AT, **fields,
            )
    assert len(_rows(db)) == 1


@pytest.mark.parametrize("kind", ["hold_cleared", "reply_end", "thread_end", "marked", "accepted"])
def test_only_card_outcomes_reach_the_plan_writer(db, kind):
    node_id = _node(db)
    with pytest.raises(ValueError, match="not a plan update outcome"):
        _plan_update(db, f"event-{kind}", node_id, kind=kind)
    assert _rows(db) == []


def test_a_hold_cleared_between_two_blocks_leaves_both_blocks_as_updates(db):
    node_id = _node(db)
    _plan_update(db, "hold-placed-1", node_id, kind="blocked")
    with pytest.raises(ValueError):
        _plan_update(db, "hold-cleared-1", node_id, kind="hold_cleared")
    _plan_update(db, "hold-placed-2", node_id, kind="blocked")

    assert [(row["kind"], row["source_id"]) for row in _rows(db)] == [
        ("blocked", "hold-placed-1"), ("blocked", "hold-placed-2"),
    ]


def test_the_title_is_snapshotted_and_survives_rename_and_node_deletion(db):
    node_id = _node(db, title="  Ship the export  ")
    _plan_update(db, "event-1", node_id)
    db.execute("UPDATE plan_nodes SET title='Renamed later' WHERE id=?", (node_id,))
    _plan_update(db, "event-2", node_id, kind="closed")
    db.execute("DELETE FROM plan_nodes WHERE id=?", (node_id,))

    assert [(row["title"], row["node_id"]) for row in _rows(db)] == [
        ("Ship the export", node_id), ("Renamed later", node_id),
    ]


def test_a_blank_or_missing_title_is_refused_not_committed(db):
    node_id = _node(db)
    # The plan schema already refuses a blank title; the index refuses one too.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE plan_nodes SET title='   ' WHERE id=?", (node_id,))
    with pytest.raises(ValueError, match="no readable title"):
        _plan_update(db, "event-gone", "no-such-node")
    with pytest.raises(sqlite3.IntegrityError):
        thread_updates._record(
            db, thread_id="owner", kind="done", source_kind="plan_event", source_id="event-blank",
            node_id=node_id, title=" ", created_at=_AT,
        )
    assert _rows(db) == []


def test_no_raw_reason_evidence_or_feedback_is_stored_or_read(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(thread_updates)")}
    assert columns == {
        "id", "thread_id", "kind", "source_kind", "source_id", "node_id", "title",
        "after_seq", "created_at",
    }
    node_id = _node(db)
    _plan_update(db, "event-1", node_id)
    card = thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]
    assert set(card) == {
        "id", "thread_id", "kind", "message_id", "node_id", "title", "after_seq", "created_at",
        "node_exists", "asks_user", "blocked_asks_user", "ask",
    }


def test_an_update_before_any_message_sits_at_zero_and_is_on_the_first_page(db):
    node_id = _node(db)
    _plan_update(db, "early", node_id)
    _message(db, "owner", 1)
    _message(db, "owner", 2)
    _plan_update(db, "later", node_id, kind="failed")

    assert [(row["source_id"], row["after_seq"]) for row in _rows(db)] == [("early", 0), ("later", 2)]
    first_page, has_more = thread_updates.read_newest_cards(db, "owner", min_seq=0)
    assert has_more is False
    assert [card["title"] for card in first_page] == ["Ship the export", "Ship the export"]
    assert [card["kind"] for card in thread_updates.read_newest_cards(db, "owner", min_seq=2)[0]] == [
        "failed",
    ]


def test_marked_updates_are_their_own_messages_and_never_cards(db):
    message_id = _message(db, "owner", 1)
    thread_updates.record_marked(
        db, thread_id="owner", message_id=message_id, node_id=None, created_at=_AT,
    )
    assert thread_updates.read_newest_cards(db, "owner", min_seq=0) == ([], False)
    latest = thread_updates.latest_by_thread(db, ["owner"])["owner"]
    assert (latest["kind"], latest["message_id"], latest["after_seq"], latest["title"]) == (
        "marked", message_id, 1, None,
    )


def test_sidebar_summary_uses_readable_marked_message_text_only(db):
    message_id = _message(db, "owner", 1)
    db.execute(
        "UPDATE messages SET content=? WHERE id=?",
        ("```text\nnot sidebar copy\n```\n## **First readable line**\nSecond line", message_id),
    )
    thread_updates.record_marked(
        db, thread_id="owner", message_id=message_id, node_id=None, created_at=_AT,
    )
    assert thread_updates.latest_by_thread(db, ["owner"])["owner"]["summary"] == "First readable line"

    node_id = _node(db)
    _plan_update(db, "failed-event", node_id, kind="failed", at="2026-09-29T11:00:00")
    assert thread_updates.latest_by_thread(db, ["owner"])["owner"]["summary"] == "Failed: Ship the export"

    thread_updates.record_marked(
        db, thread_id="worker", message_id="deleted", node_id=None, created_at=_AT,
    )
    assert thread_updates.latest_by_thread(db, ["worker"])["worker"]["summary"] is None


def test_latest_display_preview_and_attention_are_independent_for_passive_outcomes(db):
    message_id = _message(db, "owner", 1)
    db.execute("UPDATE messages SET content='Needs a decision' WHERE id=?", (message_id,))
    thread_updates.record_marked(
        db, thread_id="owner", message_id=message_id, node_id=None, created_at="2026-09-29T10:00:00",
    )
    node_id = _node(db, title="Publish the guide")
    _plan_update(db, "completion", node_id, kind="done_follow_up", at="2026-09-29T11:00:00")

    display, attention = thread_updates.latest_by_thread_with_attention(db, ["owner"])

    assert display["owner"]["summary"] == "Completed: Publish the guide · Follow-up added"
    assert attention["owner"]["summary"] == "Needs a decision"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("**Status:** deployment complete", "Status: deployment complete"),
        ("**_Complete_**", "Complete"),
        ("snake_case_value is preserved", "snake_case_value is preserved"),
        ("[Release notes](https://example.test) are ready", "Release notes are ready"),
    ],
)
def test_sidebar_summary_strips_inline_markdown_without_changing_words(content, expected):
    assert thread_updates._first_readable_line(content) == expected


def test_latest_is_one_row_per_named_thread_and_reads_are_bounded(db):
    node_id = _node(db)
    _plan_update(db, "older", node_id, at="2026-09-29T09:00:00")
    _plan_update(db, "newer", node_id, kind="failed", at="2026-09-29T11:00:00")
    _plan_update(db, "same-time-later-id", node_id, kind="closed", at="2026-09-29T11:00:00")

    latest = thread_updates.latest_by_thread(db, ["owner", "other", "owner"])
    assert list(latest) == ["owner"]
    assert latest["owner"]["kind"] == "closed"

    with pytest.raises(ValueError, match="at most 200"):
        thread_updates.latest_by_thread(db, [f"t{index}" for index in range(201)])
    cards, has_more = thread_updates.read_newest_cards(db, "owner", min_seq=0)
    assert has_more is False and len(cards) == 3


def test_the_whole_named_list_reads_in_one_statement_with_the_same_newest_rows(db):
    node_id = _node(db)
    _plan_update(db, "owner-older", node_id, at="2026-09-29T09:00:00")
    _plan_update(db, "owner-newer", node_id, kind="failed", at="2026-09-29T11:00:00")
    _plan_update(db, "worker-tie-low-id", node_id, kind="closed", at=_AT, thread_id="worker")
    _plan_update(db, "worker-tie-high-id", node_id, kind="blocked", at=_AT, thread_id="worker")
    named = ["owner", "worker", "other"] + [
        f"no-updates-{index}" for index in range(thread_updates.THREAD_LIMIT - 3)
    ]

    counter = _StatementCounter(db)
    latest = thread_updates.latest_by_thread(counter, named)

    assert len(counter.statements) == 1, counter.statements
    assert "WHERE thread_id IN (" in counter.statements[0]
    assert list(latest) == ["owner", "worker"]
    assert [(row["kind"], row["title"], row["summary"]) for row in latest.values()] == [
        ("failed", "Ship the export", "Failed: Ship the export"),
        ("blocked", "Ship the export", "Blocked: Ship the export"),
    ]
    reference = {thread_id: _newest_reference(db, thread_id) for thread_id in named}
    assert {key: {**row, "summary": None} for key, row in latest.items()} == {
        thread_id: row for thread_id, row in reference.items() if row is not None
    }

    with pytest.raises(ValueError, match="at most 200"):
        thread_updates.latest_by_thread(counter, named + ["one-too-many"])
    assert thread_updates.latest_by_thread(counter, []) == {}
    assert len(counter.statements) == 1  # neither a refused read nor an empty list queried


def test_the_reads_seek_an_index_instead_of_scanning(db):
    for index in range(300):
        db.execute(
            "INSERT INTO thread_updates (thread_id,kind,source_kind,source_id,node_id,title,"
            "after_seq,created_at) VALUES (?, 'done', 'plan_event', ?, 'n', 'T', ?, ?)",
            ("other" if index % 2 else "owner", f"e{index}", index, f"2026-09-29T10:{index % 60:02d}:00"),
        )
    db.execute("ANALYZE")
    reads = {
        "latest": (
            "SELECT id FROM thread_updates WHERE thread_id IN (?)"
            " GROUP BY thread_id HAVING (created_at || '/' || printf('%019d', id)) ="
            " MAX(created_at || '/' || printf('%019d', id))",
            ("owner",),
        ),
        "cards": (
            "SELECT id FROM thread_updates WHERE thread_id=? AND after_seq >= ? AND after_seq <= ?"
            " AND kind != 'marked' ORDER BY after_seq, id LIMIT ?",
            ("owner", 0, 50, 100),
        ),
        "node": ("SELECT id FROM thread_updates WHERE node_id=? ORDER BY id", ("n",)),
    }
    for name, (sql, args) in reads.items():
        plan = " ".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "USING INDEX idx_thread_updates" in plan or "USING COVERING INDEX" in plan, (name, plan)
        assert "SCAN" not in plan, (name, plan)
        assert "TEMP B-TREE" not in plan, (name, plan)


def test_deleting_a_thread_family_removes_every_members_updates_first(db):
    from core.thread_family import delete_thread_family, thread_family_ids

    node_id = _node(db)
    _plan_update(db, "owner-event", node_id)
    for thread_id in ("owner", "worker", "other"):
        thread_updates.record_marked(
            db, thread_id=thread_id, message_id=f"{thread_id}-sent", node_id=None, created_at=_AT,
        )
    db.execute("PRAGMA foreign_keys=OFF")
    # Each thread row may go only once its updates are gone.
    db.execute(
        """CREATE TEMP TRIGGER updates_go_first BEFORE DELETE ON threads
           WHEN EXISTS (SELECT 1 FROM thread_updates WHERE thread_id=OLD.id)
           BEGIN SELECT RAISE(ABORT, 'thread_updates left behind'); END"""
    )

    family = thread_family_ids(db, "owner")
    assert set(family) == {"owner", "worker"}
    delete_thread_family(db, family)

    assert {row["thread_id"] for row in _rows(db)} == {"other"}
    assert [row["id"] for row in db.execute("SELECT id FROM threads")] == ["other"]



def test_purging_a_thread_removes_its_updates_and_keeps_other_threads(db):
    from datetime import datetime, timezone

    from core.thread_lifecycle import _apply_purge

    node_id = _node(db)
    _plan_update(db, "owner-event", node_id)
    for thread_id in ("owner", "other"):
        thread_updates.record_marked(
            db, thread_id=thread_id, message_id=f"{thread_id}-sent", node_id=None, created_at=_AT,
        )

    _apply_purge(db, "owner", datetime(2026, 9, 29, tzinfo=timezone.utc))

    assert [(row["thread_id"], row["source_id"]) for row in _rows(db)] == [("other", "other-sent")]


def _assign(db, node_id: str, assignee: str | None) -> None:
    db.execute("UPDATE plan_nodes SET assignee=? WHERE id=?", (assignee, node_id))


def _fail(db, node_id: str) -> None:
    db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (node_id,))


def test_a_card_says_whether_its_item_is_assigned_to_the_user_now(db):
    agent_owned, user_owned, archived = _node(db), _node(db), _node(db)
    _assign(db, user_owned, "user")
    _assign(db, archived, "user")
    for index, node_id in enumerate((agent_owned, user_owned, archived)):
        _fail(db, node_id)
        _plan_update(db, f"f{index}", node_id, kind="failed")
    db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (archived,))

    cards, _more = thread_updates.read_newest_cards(db, "owner", min_seq=0)

    assert [(card["node_id"], card["asks_user"]) for card in cards] == [
        (agent_owned, False), (user_owned, True), (archived, False),
    ]
    _assign(db, agent_owned, "user")
    assert thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]["asks_user"] is True


def test_only_a_failure_on_a_user_assigned_item_can_light_the_update_dot(db):
    agent_owned, user_owned = _node(db), _node(db)
    _assign(db, user_owned, "user")
    _fail(db, user_owned)
    _fail(db, agent_owned)
    _plan_update(db, "user-fail", user_owned, kind="failed", at="2026-09-29T10:00:00")
    _plan_update(db, "agent-fail", agent_owned, kind="failed", at="2026-09-29T11:00:00")
    _plan_update(db, "done", agent_owned, kind="done", at="2026-09-29T12:00:00")

    display, attention = thread_updates.latest_by_thread_with_attention(db, ["owner"])

    assert display["owner"]["kind"] == "done"
    assert attention["owner"]["node_id"] == user_owned
    _assign(db, user_owned, None)
    assert "owner" not in thread_updates.latest_by_thread_with_attention(db, ["owner"])[1]


def _closed_parent_of(db, node_id: str) -> None:
    parent = _node(db, title="Parent")
    db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (parent,))
    db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (parent, node_id))


def _retried(db, node_id: str) -> None:
    db.execute("UPDATE plan_nodes SET state='active' WHERE id=?", (node_id,))


def _followed_up(db, node_id: str) -> None:
    follow_up = _node(db, title="Follow-up")
    db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (node_id, follow_up))


def _block(db, node_id: str, hold_id: str = "hold-1") -> None:
    db.execute(
        "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at)"
        " VALUES (?,?,'blocked','needs_input','needs input','user','test',?)",
        (hold_id, node_id, _AT),
    )


def _blocked_ask(db) -> bool:
    return thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]["blocked_asks_user"]


def test_a_blocked_card_asks_only_while_its_item_is_blocked_on_the_user(db):
    node_id = _node(db)
    _assign(db, node_id, "user")
    _block(db, node_id)
    _plan_update(db, "blocked", node_id, kind="blocked")

    assert _blocked_ask(db) is True
    assert thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]["asks_user"] is False

    db.execute("UPDATE plan_node_holds SET cleared_at='t', cleared_by_actor_id='a', clearance_event_id='e'")
    assert _blocked_ask(db) is False


def test_a_blocked_card_on_an_agent_owned_item_does_not_ask(db):
    node_id = _node(db)
    _assign(db, node_id, "subagent")
    _block(db, node_id)
    _plan_update(db, "blocked", node_id, kind="blocked")

    assert _blocked_ask(db) is False
    _assign(db, node_id, "user")
    assert _blocked_ask(db) is True


@pytest.mark.parametrize("settle", [_closed_parent_of, lambda db, node: db.execute(
    "UPDATE plan_nodes SET archived_at='t' WHERE id=?", (node,))])
def test_a_blocked_card_in_a_retired_branch_or_archived_does_not_ask(db, settle):
    node_id = _node(db)
    _assign(db, node_id, "user")
    _block(db, node_id)
    _plan_update(db, "blocked", node_id, kind="blocked")
    assert _blocked_ask(db) is True

    settle(db, node_id)

    assert _blocked_ask(db) is False


@pytest.mark.parametrize("settle", [_retried, _followed_up, _closed_parent_of])
def test_a_user_failure_that_no_longer_asks_sets_no_dot_and_no_standalone_card(db, settle):
    node_id = _node(db)
    _assign(db, node_id, "user")
    _fail(db, node_id)
    _plan_update(db, "user-fail", node_id, kind="failed")
    assert thread_updates.latest_by_thread_with_attention(db, ["owner"])[1]["owner"]["node_id"] == node_id
    assert thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]["asks_user"] is True

    settle(db, node_id)

    assert "owner" not in thread_updates.latest_by_thread_with_attention(db, ["owner"])[1]
    assert thread_updates.read_newest_cards(db, "owner", min_seq=0)[0][0]["asks_user"] is False


def test_an_account_move_row_changes_no_update_count_card_or_dot(db):
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq)"
        " VALUES ('move','owner','system','Moved to Claude Code · B',?,?,1)",
        ('{"type":"account_switch","reason":"account limit"}', _AT),
    )

    assert thread_updates.read_newest_cards(db, "owner", min_seq=0) == ([], False)
    display, attention = thread_updates.latest_by_thread_with_attention(db, ["owner"])
    assert display == {} and attention == {}
    assert thread_updates.seen_by_thread(db, "user", {}, ["owner"]) == {}
