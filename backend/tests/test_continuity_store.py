"""Continuity entries are addressable, and retirement leaves a trace.

The failure this store exists to prevent is a constraint that disappears with
nobody able to say when or why. So the tests hold two lines: an entry can be
named and edited by id, and it cannot leave silently.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.continuity_schema import CONTINUITY_DDL
from infra.continuity_store import (
    ContinuityConflict,
    ContinuityError,
    active_entries,
    add_entry,
    delete_thread_continuity,
    replace_entry,
    retire_entry,
    store_revision,
)
from core.continuity_types import MAX_LIST_ITEMS

EPOCH = 1


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO threads (id) VALUES ('t1')")
    for statement in CONTINUITY_DDL:
        conn.execute(statement)
    return conn


def _statement(text: str) -> dict:
    return {"statement": text, "source_refs": []}


def _add(conn, field="active_constraints", text="Hold merges to dev") -> int:
    return add_entry(
        conn, thread_id="t1", history_epoch=EPOCH, field=field, body=_statement(text)
    )


def test_entries_get_small_addressable_ids():
    conn = _db()
    first = _add(conn, text="one")
    second = _add(conn, text="two")

    # Small integers, so a model can say "drop 2" in a tool call.
    assert (first, second) == (1, 2)


def test_active_entries_are_grouped_by_checkpoint_field():
    conn = _db()
    _add(conn, field="active_constraints", text="constraint")
    _add(conn, field="facts", text="a fact")

    grouped = active_entries(conn, "t1", EPOCH)

    assert [e["statement"] for e in grouped["active_constraints"]] == ["constraint"]
    assert [e["statement"] for e in grouped["facts"]] == ["a fact"]
    assert grouped["decisions"] == []


def test_entry_id_travels_with_the_body():
    conn = _db()
    entry_id = _add(conn, text="addressable")

    entry = active_entries(conn, "t1", EPOCH)["active_constraints"][0]

    assert entry["entry_id"] == entry_id


def test_replace_keeps_the_id():
    conn = _db()
    entry_id = _add(conn, text="before")

    replace_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        body=_statement("after"),
    )

    entries = active_entries(conn, "t1", EPOCH)["active_constraints"]
    assert len(entries) == 1
    assert entries[0]["entry_id"] == entry_id
    assert entries[0]["statement"] == "after"


def test_retired_entry_leaves_the_active_set():
    conn = _db()
    entry_id = _add(conn)

    retire_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        reason="the release promoted",
        source_ref="message-42",
    )

    assert active_entries(conn, "t1", EPOCH)["active_constraints"] == []


def test_retirement_is_tombstoned_not_deleted():
    conn = _db()
    entry_id = _add(conn)

    retire_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        reason="the release promoted",
        source_ref="message-42",
    )

    row = conn.execute(
        "SELECT state, retired_reason, retired_source_ref FROM continuity_entries "
        "WHERE entry_id=?",
        (entry_id,),
    ).fetchone()
    # The whole point: a removal that leaves no trace is the bug.
    assert row["state"] == "retired"
    assert row["retired_reason"] == "the release promoted"
    assert row["retired_source_ref"] == "message-42"


def test_retiring_a_constraint_requires_evidence_not_just_a_reason():
    conn = _db()
    entry_id = _add(conn)

    with pytest.raises(ContinuityError, match="source_ref"):
        retire_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            entry_id=entry_id,
            reason="superseded",
        )

    # Still binding, because a plausible sentence is not proof.
    assert len(active_entries(conn, "t1", EPOCH)["active_constraints"]) == 1


def test_ordinary_fields_retire_on_a_reason_alone():
    conn = _db()
    entry_id = _add(conn, field="facts", text="a stale fact")

    retire_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        reason="no longer true",
    )

    assert active_entries(conn, "t1", EPOCH)["facts"] == []


def test_retirement_requires_a_reason():
    conn = _db()
    entry_id = _add(conn, field="facts")

    with pytest.raises(ContinuityError, match="reason"):
        retire_entry(
            conn, thread_id="t1", history_epoch=EPOCH, entry_id=entry_id, reason="  "
        )


def test_retiring_twice_is_not_an_error():
    conn = _db()
    entry_id = _add(conn, field="facts")
    kwargs = dict(
        thread_id="t1", history_epoch=EPOCH, entry_id=entry_id, reason="done"
    )

    retire_entry(conn, **kwargs)
    retire_entry(conn, **kwargs)  # A replayed edit must not fail.

    assert active_entries(conn, "t1", EPOCH)["facts"] == []


def test_store_revision_advances_on_every_write():
    conn = _db()
    assert store_revision(conn, "t1", EPOCH) == 0

    entry_id = _add(conn)
    after_add = store_revision(conn, "t1", EPOCH)
    replace_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        body=_statement("changed"),
    )
    after_replace = store_revision(conn, "t1", EPOCH)

    # This is what a staged fold patch is compared against.
    assert 0 < after_add < after_replace


def test_replace_rejects_a_stale_read():
    conn = _db()
    entry_id = _add(conn, text="original")
    replace_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        body=_statement("first writer"),
    )

    with pytest.raises(ContinuityConflict):
        replace_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            entry_id=entry_id,
            body=_statement("second writer"),
            expected_revision=1,
        )


def test_replacing_a_retired_entry_is_refused():
    conn = _db()
    entry_id = _add(conn, field="facts")
    retire_entry(
        conn, thread_id="t1", history_epoch=EPOCH, entry_id=entry_id, reason="done"
    )

    with pytest.raises(ContinuityError, match="retired"):
        replace_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            entry_id=entry_id,
            body=_statement("resurrected"),
        )


def test_replace_accepts_a_matching_expected_field():
    conn = _db()
    entry_id = _add(conn, field="facts", text="before")

    replace_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        body=_statement("after"),
        expected_field="facts",
    )

    assert active_entries(conn, "t1", EPOCH)["facts"][0]["statement"] == "after"


def test_replace_rejects_a_mismatched_expected_field():
    conn = _db()
    entry_id = _add(conn, field="facts", text="before")

    with pytest.raises(
        ContinuityError,
        match=f"field decisions does not match entry {entry_id}'s field facts",
    ):
        replace_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            entry_id=entry_id,
            body=_statement("after"),
            expected_field="decisions",
        )


def test_retire_accepts_a_matching_expected_field():
    conn = _db()
    entry_id = _add(conn, field="facts")

    retire_entry(
        conn,
        thread_id="t1",
        history_epoch=EPOCH,
        entry_id=entry_id,
        reason="done",
        expected_field="facts",
    )

    assert active_entries(conn, "t1", EPOCH)["facts"] == []


def test_retire_rejects_a_mismatched_expected_field():
    conn = _db()
    entry_id = _add(conn, field="facts")

    with pytest.raises(
        ContinuityError,
        match=f"field decisions does not match entry {entry_id}'s field facts",
    ):
        retire_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            entry_id=entry_id,
            reason="done",
            expected_field="decisions",
        )

    # A rejected mismatch must not retire the entry.
    assert len(active_entries(conn, "t1", EPOCH)["facts"]) == 1


def test_unknown_entry_is_refused():
    conn = _db()
    with pytest.raises(ContinuityError, match="does not exist"):
        replace_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            entry_id=99,
            body=_statement("ghost"),
        )


def test_unknown_field_is_refused():
    conn = _db()
    with pytest.raises(ContinuityError, match="unknown continuity field"):
        add_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            field="objective",  # a scalar, not an entry list
            body=_statement("nope"),
        )


def test_body_is_validated_against_the_checkpoint_schema():
    conn = _db()
    with pytest.raises(ContinuityError, match="invalid decisions entry"):
        add_entry(
            conn,
            thread_id="t1",
            history_epoch=EPOCH,
            field="decisions",
            body=_statement("a decision needs a rationale"),
        )


def test_the_list_bound_fails_loudly_rather_than_at_checkpoint_time():
    conn = _db()
    for index in range(MAX_LIST_ITEMS):
        _add(conn, field="facts", text=f"fact-{index}")

    with pytest.raises(ContinuityError, match="maximum"):
        _add(conn, field="facts", text="one too many")


def test_retiring_frees_room_under_the_bound():
    conn = _db()
    ids = [_add(conn, field="facts", text=f"fact-{i}") for i in range(MAX_LIST_ITEMS)]
    retire_entry(
        conn, thread_id="t1", history_epoch=EPOCH, entry_id=ids[0], reason="stale"
    )

    _add(conn, field="facts", text="now there is room")

    assert len(active_entries(conn, "t1", EPOCH)["facts"]) == MAX_LIST_ITEMS


def test_a_new_epoch_starts_clean():
    conn = _db()
    _add(conn, text="epoch one")

    assert active_entries(conn, "t1", EPOCH + 1)["active_constraints"] == []


def test_thread_delete_cascades():
    conn = _db()
    _add(conn)

    conn.execute("DELETE FROM threads WHERE id='t1'")

    assert conn.execute("SELECT COUNT(*) FROM continuity_entries").fetchone()[0] == 0


def test_purge_drops_continuity_while_the_session_row_survives():
    conn = _db()
    conn.execute("""CREATE TABLE lesson_placement_checks (
        thread_id TEXT, history_epoch INTEGER, entry_id INTEGER, revision INTEGER,
        check_name TEXT, flag TEXT, override_reason TEXT, created_at TEXT
    )""")
    entry = _add(conn)
    conn.execute(
        """INSERT INTO lesson_placement_checks(
            thread_id,history_epoch,entry_id,revision,check_name,flag,created_at
        ) VALUES ('t1',0,? ,1,'incident-details','date','now')""", (entry,),
    )

    # Purge keeps the session row, so the cascade never fires and this explicit
    # delete is the only thing stopping condensed content outliving its thread.
    delete_thread_continuity(conn, "t1")

    assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM continuity_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM continuity_store").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lesson_placement_checks").fetchone()[0] == 0
